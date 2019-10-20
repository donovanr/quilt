from codecs import iterdecode
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import pathlib
import platform
import shutil
from threading import Lock
from urllib.parse import quote, unquote, urlparse
import warnings

from botocore import UNSIGNED
from botocore.client import Config
from botocore.exceptions import ClientError
import boto3
from boto3.s3.transfer import TransferConfig
from s3transfer.utils import OSUtils, signal_transferring, signal_not_transferring

import jsonlines
from tqdm import tqdm

from .session import create_botocore_session
from .util import QuiltException, make_s3_url, parse_file_url, parse_s3_url
from . import xattr


HELIUM_METADATA = 'helium'
HELIUM_XATTR = 'com.quiltdata.helium'


if platform.system() == 'Linux':
    # Linux only allows users to modify user.* xattrs.
    HELIUM_XATTR = 'user.%s' % HELIUM_XATTR


def create_s3_client():
    botocore_session = create_botocore_session()
    boto_session = boto3.Session(botocore_session=botocore_session)


    # Check whether credentials are present
    # if boto_session.get_credentials() is None:
        # Use unsigned boto if credentials aren't present
        # print("got unsigned credentials")
        # print(boto_session.client('sts').get_caller_identity())
        # s3_client = boto_session.client('s3', config=Config(signature_version=UNSIGNED))
    # else:
        # Use normal boto
        # print("got normal credentials")
        # print(boto3.Session(botocore_session=botocore_session).client('sts').get_caller_identity())
        # s3_client = boto_session.client('s3')

    s3_client = boto3.client('s3')

    # Enable/disable file read callbacks when uploading files.
    # Copied from https://github.com/boto/s3transfer/blob/develop/s3transfer/manager.py#L501
    event_name = 'request-created.s3'
    s3_client.meta.events.register_first(
        event_name, signal_not_transferring,
        unique_id='datatransfer-not-transferring')
    s3_client.meta.events.register_last(
        event_name, signal_transferring,
        unique_id='datatransfer-transferring')

    return s3_client


s3_transfer_config = TransferConfig()
s3_threads = 4

# When uploading files at least this size, compare the ETags first and skip the upload if they're equal;
# copy the remote file onto itself if the metadata changes.
UPLOAD_ETAG_OPTIMIZATION_THRESHOLD = 1024


def _parse_metadata(resp):
    return json.loads(resp['Metadata'].get(HELIUM_METADATA, '{}'))

def _parse_file_metadata(path):
    try:
        meta_bytes = xattr.getxattr(path, HELIUM_XATTR)
        meta = json.loads(meta_bytes.decode('utf-8'))
    except IOError:
        # No metadata
        meta = {}
    return meta



def _copy_local_file(src_path, dest_path, override_meta):
    pathlib.Path(dest_path).parent.mkdir(parents=True, exist_ok=True)

    # TODO(dima): More detailed progress.
    shutil.copyfile(src_path, dest_path)
    shutil.copymode(src_path, dest_path)

    if override_meta is None:
        meta = _parse_file_metadata(src_path)
    else:
        meta = override_meta

    xattr.setxattr(dest_path, HELIUM_XATTR, json.dumps(meta).encode('utf-8'))


def _upload_file(size, src_path, dest_bucket, dest_key, override_meta):
    s3_client = create_s3_client()
    if override_meta is None:
        meta = _parse_file_metadata(src_path)
    else:
        meta = override_meta


    if size < s3_transfer_config.multipart_threshold:
        with OSUtils().open_file_chunk_reader(src_path, 0, size, []) as fd:
            try:
                # s3_client = boto3.client('s3')
                resp = s3_client.put_object(
                    Body=fd,
                    Bucket=dest_bucket,
                    Key=dest_key,
                    Metadata={HELIUM_METADATA: json.dumps(meta)},
                )
            except Exception as ex:
                print("failure: ", "dest:", dest_bucket, dest_key)
                print(boto3.client('sts').get_caller_identity())
                print(ex)
                raise(ex)

        version_id = resp.get('VersionId')  # Absent in unversioned buckets.
        out_key = dest_key
    else:
        resp = s3_client.create_multipart_upload(
            Bucket=dest_bucket,
            Key=dest_key,
            Metadata={HELIUM_METADATA: json.dumps(meta)},
        )
        upload_id = resp['UploadId']

        chunk_offsets = list(range(0, size, s3_transfer_config.multipart_chunksize))

        parts = []


        for i, start in enumerate(chunk_offsets):
            end = min(start + s3_transfer_config.multipart_chunksize, size)

            part_id = i + 1
            with OSUtils().open_file_chunk_reader(src_path, start, end-start, []) as fd:
                part = s3_client.upload_part(
                    Body=fd,
                    Bucket=dest_bucket,
                    Key=dest_key,
                    UploadId=upload_id,
                    PartNumber=part_id
                )
            parts.append({"PartNumber": part_id, "ETag": part["ETag"]})

        resp = s3_client.complete_multipart_upload(
            Bucket=dest_bucket,
            Key=dest_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts}
        )
        out_key = resp['Key']
        version_id = resp.get('VersionId')  # Absent in unversioned buckets.


    return f"s3://{dest_bucket}/{out_key}?versionId={version_id}"


def _download_file(src_bucket, src_key, src_version, dest_path, override_meta):
    dest_file = pathlib.Path(dest_path)
    if dest_file.is_reserved():
        raise ValueError("Cannot download to %r: reserved file name" % dest_path)

    s3_client = create_s3_client()

    dest_file.parent.mkdir(parents=True, exist_ok=True)

    params = dict(Bucket=src_bucket, Key=src_key)
    if src_version is not None:
        params.update(dict(VersionId=src_version))
    resp = s3_client.get_object(**params)

    if override_meta is None:
        meta = _parse_metadata(resp)
    else:
        meta = override_meta

    body = resp['Body']
    with open(dest_path, 'wb') as fd:
        while True:
            chunk = body.read(1024)
            if not chunk:
                break
            fd.write(chunk)
            # ctx.progress(len(chunk))

    try:
        xattr.setxattr(dest_path, HELIUM_XATTR, json.dumps(meta).encode('utf-8'))
    except OSError:
        # this indicates that the destination path is on an OS that doesn't support xattrs
        # if this is the case, raise a warning and leave xattrs blank
        warnings.warn(
            f"Unable to write file metadata to xattrs for destination {dest_path!r} - operation "
            f"not permitted or supported by the OS. Your OS either doesn't support extended "
            f"file attributes in this directory, or has them disabled."
        )



def _copy_remote_file(size, src_bucket, src_key, src_version,
                      dest_bucket, dest_key, override_meta, extra_args=None):
    src_params = dict(
        Bucket=src_bucket,
        Key=src_key
    )
    if src_version is not None:
        src_params.update(
            VersionId=src_version
        )

    s3_client = create_s3_client()

    if size < s3_transfer_config.multipart_threshold:
        params = dict(
            CopySource=src_params,
            Bucket=dest_bucket,
            Key=dest_key
        )
        if override_meta is None:
            params.update(dict(
                MetadataDirective='COPY'
            ))
        else:
            params.update(dict(
                MetadataDirective='REPLACE',
                Metadata={HELIUM_METADATA: json.dumps(override_meta)}
            ))

        if extra_args:
            params.update(extra_args)

        resp = s3_client.copy_object(**params)
        version_id = resp.get('VersionId')  # Absent in unversioned buckets.
    else:
        if override_meta is None:
            resp = s3_client.head_object(Bucket=src_bucket, Key=src_key)
            metadata = resp['Metadata']
        else:
            metadata = {HELIUM_METADATA: json.dumps(override_meta)}
        resp = s3_client.create_multipart_upload(
            Bucket=dest_bucket,
            Key=dest_key,
            Metadata=metadata,
        )
        upload_id = resp['UploadId']

        chunk_offsets = list(range(0, size, s3_transfer_config.multipart_chunksize))

        lock = Lock()
        remaining = len(chunk_offsets)
        parts = [None] * remaining

        def upload_part(i, start, end):
            nonlocal remaining
            part_id = i + 1
            part = s3_client.upload_part_copy(
                CopySource=src_params,
                CopySourceRange=f'bytes={start}-{end-1}',
                Bucket=dest_bucket,
                Key=dest_key,
                UploadId=upload_id,
                PartNumber=part_id
            )
            with lock:
                parts[i] = {"PartNumber": part_id, "ETag": part["CopyPartResult"]["ETag"]}
                remaining -= 1
                done = remaining == 0


            if done:
                resp = s3_client.complete_multipart_upload(
                    Bucket=dest_bucket,
                    Key=dest_key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts}
                )
                version_id = resp.get('VersionId')  # Absent in unversioned buckets.

        for i, start in enumerate(chunk_offsets):
            end = min(start + s3_transfer_config.multipart_chunksize, size)
            upload_part(i, start, end)


def _upload_or_copy_file(s3_client, size, src_path, dest_bucket, dest_path, override_meta):


    # Optimization: check if the remote file already exists and has the right ETag,
    # and skip the upload.
    if size >= UPLOAD_ETAG_OPTIMIZATION_THRESHOLD:
        try:
            resp = s3_client.head_object(Bucket=dest_bucket, Key=dest_path)
        except ClientError:
            # Destination doesn't exist, so fall through to the normal upload.
            pass
        else:
            # Check the ETag.
            dest_size = resp['ContentLength']
            dest_etag = resp['ETag']
            dest_version_id = resp['VersionId']
            dest_meta = _parse_metadata(resp)
            if size == dest_size:
                src_etag = _calculate_etag(src_path)
                if src_etag == dest_etag:
                    if override_meta is None or override_meta == dest_meta:
                        # Nothing more to do. We should not attempt to copy the object because
                        # that would cause the "copy object to itself" error.
                        pass
                    else:
                        # NOTE(dima): There is technically a race condition here: if the S3 file
                        # got modified after the `head_object` call AND we have no version ID,
                        # we could end up with mismatched body and metadata. It makes no sense
                        # for the user to perform such actions, but just in case, pass
                        # CopySourceIfMatch to make the request fail.
                        extra_args = dict(CopySourceIfMatch=src_etag)
                        _copy_remote_file(
                            size, dest_bucket, dest_path, dest_version_id,
                            dest_bucket, dest_path, override_meta, extra_args
                        )
                    return f"s3://{dest_bucket}/{dest_path}?versionId={dest_version_id}"

    # If the optimization didn't happen, do the normal upload.
    return _upload_file(size, src_path, dest_bucket, dest_path, override_meta)



def worker(list_of_arg_tuples):
    # A single set of arguments is: src_url, dest_url, size, override_meta = args
    # We pass in batches to reduce s3_client related overhead
    s3_client = create_s3_client()

    for args in list_of_arg_tuples:
        assert len(args) == 5

    returns = []
    for args in list_of_arg_tuples:
        src_url, dest_url, size, override_meta, q = args



        if src_url.scheme == 'file':
            src_path = parse_file_url(src_url)
            if dest_url.scheme == 'file':
                raise NotImplementedError()
                # dest_path = parse_file_url(dest_url)
                # _copy_local_file(src_path, dest_path, override_meta)
            elif dest_url.scheme == 's3':
                dest_bucket, dest_path, dest_version_id = parse_s3_url(dest_url)
                if dest_version_id:
                    raise ValueError("Cannot set VersionId on destination")
                file_s3_path = _upload_or_copy_file(s3_client, size, src_path, dest_bucket, dest_path, override_meta)
                q.put(size)
                returns.append(file_s3_path)
            else:
                raise NotImplementedError
        elif src_url.scheme == 's3':
            raise NotImplementedError()
            # src_bucket, src_path, src_version_id = parse_s3_url(src_url)
            # if dest_url.scheme == 'file':
            #     dest_path = parse_file_url(dest_url)
            #     _download_file(src_bucket, src_path, src_version_id, dest_path, override_meta)
            # elif dest_url.scheme == 's3':
            #     dest_bucket, dest_path, dest_version_id = parse_s3_url(dest_url)
            #     if dest_version_id:
            #         raise ValueError("Cannot set VersionId on destination")
            #     _copy_remote_file(ctx, size, src_bucket, src_path, src_version_id,
            #                       dest_bucket, dest_path, override_meta)
            # else:
            #     raise NotImplementedError
        else:
            raise NotImplementedError
    return returns

def _copy_file_list_internal(file_list):
    """
    Takes a list of tuples (src, dest, size, override_meta) and copies the data in parallel.
    Returns versioned URLs for S3 destinations and regular file URLs for files.
    """

    import multiprocessing
    from multiprocessing import Queue, Pool
    import time

    m = multiprocessing.Manager()
    shared_queue = m.Queue()

    pool_worker_count = 40
    from multiprocessing import Pool
    batched_file_lists = []
    for i in range(pool_worker_count):
        batched_file_lists.append([])
    for i, file_tuple in enumerate(file_list):
        worker_id = i % pool_worker_count
        args = list(file_tuple) + [shared_queue]
        batched_file_lists[worker_id].append(args)

    SLEEP_TIME = 0.1
    total_size = sum([size for src, dest, size, override_meta in file_list])
    manual_progress = 0


    with Pool(pool_worker_count) as p:
        async_result_sets = p.map_async(worker, batched_file_lists)
        with tqdm(desc="Pushing", total=total_size, unit='B', unit_scale=True) as progress:
            while True:
                try:
                    chunk_size = shared_queue.get(block=False)
                    manual_progress += chunk_size
                    # print(manual_progress, "of", total_size)
                    progress.update(chunk_size)

                    if manual_progress == total_size:
                        break
                except Exception as e:
                    # print("ex")
                    time.sleep(SLEEP_TIME)
                    continue

    flattened_results = []
    for result_set in async_result_sets:
        flattened_results.extend(result_set)
    # for idx, args in enumerate(file_list):
    #     run_task(worker, idx, *args)



    assert all(flattened_results)

    return flattened_results



def _calculate_etag(file_path):
    """
    Attempts to calculate a local file's ETag the way S3 does:
    - Normal uploads: MD5 of the file
    - Multi-part uploads: MD5 of the (binary) MD5s of the parts, dash, number of parts
    We can't know how the file was actually uploaded - but we're assuming it was done using
    the default settings, which we get from `s3_transfer_config`.
    """
    size = pathlib.Path(file_path).stat().st_size
    with open(file_path, 'rb') as fd:
        if size <= s3_transfer_config.multipart_threshold:
            contents = fd.read()
            etag = hashlib.md5(contents).hexdigest()
        else:
            hashes = []
            while True:
                contents = fd.read(s3_transfer_config.multipart_chunksize)
                if not contents:
                    break
                hashes.append(hashlib.md5(contents).digest())
            etag = '%s-%d' % (hashlib.md5(b''.join(hashes)).hexdigest(), len(hashes))
    return '"%s"' % etag


def delete_object(bucket, key):
    s3_client = create_s3_client()

    s3_client.head_object(Bucket=bucket, Key=key)  # Make sure it exists
    s3_client.delete_object(Bucket=bucket, Key=key)  # Actually delete it


def list_object_versions(bucket, prefix, recursive=True):
    if prefix and not prefix.endswith('/'):
        raise ValueError("Prefix must end with /")

    list_obj_params = dict(Bucket=bucket,
                           Prefix=prefix
                          )
    if not recursive:
        # Treat '/' as a directory separator and only return one level of files instead of everything.
        list_obj_params.update(dict(Delimiter='/'))

    # TODO: make this a generator?
    versions = []
    delete_markers = []
    prefixes = []

    s3_client = create_s3_client()
    paginator = s3_client.get_paginator('list_object_versions')

    for response in paginator.paginate(**list_obj_params):
        versions += response.get('Versions', [])
        delete_markers += response.get('DeleteMarkers', [])
        prefixes += response.get('CommonPrefixes', [])

    if recursive:
        return versions, delete_markers
    else:
        return prefixes, versions, delete_markers


def list_objects(bucket, prefix, recursive=True):
    if prefix and not prefix.endswith('/'):
        raise ValueError("Prefix must end with /")

    objects = []
    prefixes = []
    list_obj_params = dict(Bucket=bucket,
                           Prefix=prefix)
    if not recursive:
        # Treat '/' as a directory separator and only return one level of files instead of everything.
        list_obj_params.update(dict(Delimiter='/'))

    s3_client = create_s3_client()
    paginator = s3_client.get_paginator('list_objects_v2')

    for response in paginator.paginate(**list_obj_params):
        objects += response.get('Contents', [])
        prefixes += response.get('CommonPrefixes', [])

    if recursive:
        return objects
    else:
        return prefixes, objects


def _looks_like_dir(s):
    return not s or s.endswith('/')


def list_url(src):
    src_url = urlparse(src)
    if src_url.scheme == 'file':
        src_path = parse_file_url(src_url)
        src_file = pathlib.Path(src_path)

        for f in src_file.rglob('*'):
            try:
                if f.is_file():
                    size = f.stat().st_size
                    yield f.relative_to(src_file).as_posix(), size
            except FileNotFoundError:
                # If a file does not exist, is it really a file?
                pass
    elif src_url.scheme == 's3':
        src_bucket, src_path, src_version_id = parse_s3_url(src_url)
        if src_version_id is not None:
            raise ValueError(f"Directories cannot have version IDs: {src_url!r}")
        if not _looks_like_dir(src_path):
            src_path += '/'
        s3_client = create_s3_client()
        paginator = s3_client.get_paginator('list_objects_v2')
        for response in paginator.paginate(Bucket=src_bucket, Prefix=src_path):
            for obj in response.get('Contents', []):
                key = obj['Key']
                if not key.startswith(src_path):
                    raise ValueError("Unexpected key: %r" % key)
                yield key[len(src_path):], obj['Size']
    else:
        raise NotImplementedError


def delete_url(src):
    """Deletes the given URL.
    Follows S3 semantics even for local files:
    - If the URL does not exist, it's a no-op.
    - If it's a non-empty directory, it's also a no-op.
    """
    src_url = urlparse(src)
    if src_url.scheme == 'file':
        src_path = parse_file_url(src_url)
        src_file = pathlib.Path(src_path)

        if _looks_like_dir(src_path):
            try:
                src_file.rmdir()
            except OSError:
                # Ignore non-empty directories, for consistency with S3
                pass
        else:
            try:
                src_file.unlink()
            except FileExistsError:
                pass
    elif src_url.scheme == 's3':
        src_bucket, src_path, src_version_id = parse_s3_url(src_url)
        s3_client = create_s3_client()
        s3_client.delete_object(Bucket=src_bucket, Key=src_path)
    else:
        raise NotImplementedError


def copy_file_list(file_list):
    """
    Takes a list of tuples (src, dest, size, override_meta) and copies them in parallel.
    URLs must be regular files, not directories.
    Returns versioned URLs for S3 destinations and regular file URLs for files.
    """
    processed_file_list = []
    for src, dest, size, override_meta in file_list:
        src_url = urlparse(src)
        src_path = unquote(src_url.path)
        dest_url = urlparse(dest)
        dest_path = unquote(dest_url.path)

        if _looks_like_dir(src_path) or _looks_like_dir(dest_path):
            raise ValueError("Directories are not allowed")

        processed_file_list.append((src_url, dest_url, size, override_meta))

    print("Entering _copy_file_list_internal")
    return _copy_file_list_internal(processed_file_list)


def copy_file(src, dest, override_meta=None, size=None):
    """
    Copies a single file or directory.
    If src is a file, dest can be a file or a directory.
    If src is a directory, dest must be a directory.
    """
    def sanity_check(rel_path):
        for part in rel_path.split('/'):
            if part in ('', '.', '..'):
                raise ValueError("Invalid relative path: %r" % rel_path)

    def url_append(url, path):
        return url._replace(path=url.path + quote(path))

    src_url = urlparse(src)
    dest_url = urlparse(dest)
    src_path = unquote(src_url.path)
    dest_path = unquote(dest_url.path)

    url_list = []
    if _looks_like_dir(src_path):
        if not _looks_like_dir(dest_path):
            raise ValueError("Destination path must end in /")
        if override_meta is not None:
            raise ValueError("`override_meta` does not make sense for directories")
        if size is not None:
            raise ValueError("`size` does not make sense for directories")

        for rel_path, size in list_url(src):
            sanity_check(rel_path)
            new_src_url = url_append(src_url, rel_path)
            new_dest_url = url_append(dest_url, rel_path)
            url_list.append((new_src_url, new_dest_url, size, None))
        if not url_list:
            raise QuiltException("No objects to download.")
    else:
        if _looks_like_dir(dest_path):
            name = src_path.rsplit('/', 1)[1]
            dest_url = url_append(dest_url, name)
        if size is None:
            size, _, _ = get_size_and_meta(src)
        url_list.append((src_url, dest_url, size, override_meta))

    s3_client = create_s3_client()
    _copy_file_list_internal(url_list)


def put_bytes(data, dest, meta=None):
    dest_url = urlparse(dest)
    if dest_url.scheme == 'file':
        dest_path = pathlib.Path(parse_file_url(dest_url))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        if meta is not None:
            xattr.setxattr(dest_path, HELIUM_XATTR, json.dumps(meta).encode('utf-8'))
    elif dest_url.scheme == 's3':
        dest_bucket, dest_path, dest_version_id = parse_s3_url(dest_url)
        if not dest_path or dest_path.endswith('/'):
            raise ValueError("Invalid path: %r" % dest_path)
        if dest_version_id:
            raise ValueError("Cannot set VersionId on destination")
        s3_client = create_s3_client()
        s3_client.put_object(
            Bucket=dest_bucket,
            Key=dest_path,
            Body=data,
            Metadata={HELIUM_METADATA: json.dumps(meta)}
        )
    else:
        raise NotImplementedError

def get_bytes(src):
    src_url = urlparse(src)
    if src_url.scheme == 'file':
        src_path = pathlib.Path(parse_file_url(src_url))
        data = src_path.read_bytes()
        meta = _parse_file_metadata(src_path)
    elif src_url.scheme == 's3':
        src_bucket, src_path, src_version_id = parse_s3_url(src_url)
        params = dict(Bucket=src_bucket, Key=src_path)
        if src_version_id is not None:
            params.update(dict(VersionId=src_version_id))
        s3_client = create_s3_client()
        resp = s3_client.get_object(**params)
        data = resp['Body'].read()
        meta = _parse_metadata(resp)
    else:
        raise NotImplementedError
    return data, meta

def get_size_and_meta(src):
    """
    Gets metadata for the object at a given URL.

    Returns:
        size, meta(dict), version(str)
    """
    src_url = urlparse(src)
    path = unquote(src_url.path)

    if not path or path.endswith('/'):
        raise QuiltException("Invalid path: %r; cannot be a directory")

    version = None
    if src_url.scheme == 'file':
        src_path = pathlib.Path(parse_file_url(src_url))
        if not src_path.is_file():
            raise QuiltException("Not a file: %r" % str(src_path))
        size = src_path.stat().st_size
        meta = _parse_file_metadata(src_path)
    elif src_url.scheme == 's3':
        bucket, key, version_id = parse_s3_url(src_url)
        params = dict(
            Bucket=bucket,
            Key=key
        )
        if version_id:
            params.update(dict(VersionId=version_id))
        s3_client = create_s3_client()
        resp = s3_client.head_object(**params)
        size = resp['ContentLength']
        meta = _parse_metadata(resp)
        if resp.get('VersionId', 'null') != 'null':  # Yes, 'null'
            version = resp['VersionId']
    else:
        raise NotImplementedError
    return size, meta, version

def _process_url(args):
    CHUNK_THRESH = 1_000_000
    assert len(args) == 3
    src, size, q = args
    # print(args)
    src_url = urlparse(src)
    hash_obj = hashlib.sha256()
    if src_url.scheme == 'file':
        path = pathlib.Path(parse_file_url(src_url))

        with open(path, 'rb') as fd:
            # print("opened file")
            progress_counter = 0
            while True:
                # print("i")
                chunk = fd.read(1024)


                # print("j")
                if not chunk:
                    # print("loop breal")
                    break
                progress_counter += len(chunk)
                if progress_counter > CHUNK_THRESH:
                    q.put(progress_counter)
                    progress_counter = 0
                hash_obj.update(chunk)
            current_file_size = fd.tell()

            # print(current_file_size)
            if current_file_size != size:
                # print("warn")
                warnings.warn(
                        f"Expected the package entry at {src!r} to be {size} B in size, but "
                        f"found an object which is {current_file_size} B instead. This "
                        f"indicates that the content of the file changed in between when you "
                        f"included this  entry in the package (via set or set_dir) and now. "
                        f"This should be avoided if possible."
                )
            q.put(progress_counter)
            # q.put(1)
            # print("Done with file!")

    elif src_url.scheme == 's3':
        src_bucket, src_path, src_version_id = parse_s3_url(src_url)
        params = dict(Bucket=src_bucket, Key=src_path)
        if src_version_id is not None:
            params.update(dict(VersionId=src_version_id))
        s3_client = create_s3_client()
        resp = s3_client.get_object(**params)
        body = resp['Body']
        for chunk in body:
            hash_obj.update(chunk)
            q.put(len(chunk))
        print("Done!")
    else:
        # print("e")
        raise NotImplementedError
    # print("Done with entrie object!")
    # q.put(1)
    return hash_obj.hexdigest()

def calculate_sha256(src_list, sizes):
    assert len(src_list) == len(sizes)

    total_size = sum(sizes)
    import multiprocessing # multiprocessing.cpu_count
    import itertools
    from multiprocessing import Queue, Pool
    import time
    import uuid
    import os
    m = multiprocessing.Manager()
    shared_queue = m.Queue()
    manual_progress = 0
    DEFAULT_QUILT_SLEEP_TIME = 0.1
    sleep_time = os.environ.get("QUILT_SLEEP_TIME", DEFAULT_QUILT_SLEEP_TIME)
    sleep_time = float(sleep_time)

    print(total_size)
    DEFAULT_QUILT_NUM_WORKERS = multiprocessing.cpu_count()
    num_workers = os.environ.get("QUILT_NUM_WORKERS", DEFAULT_QUILT_NUM_WORKERS)
    num_workers = int(num_workers)
    print("sleep", sleep_time)
    print("workers", num_workers)
    with Pool(num_workers) as p:
        async_results = p.map_async(_process_url, zip(src_list, sizes, itertools.repeat(shared_queue)))
        with tqdm(desc="Hashing", total=total_size, unit='B', unit_scale=True) as progress:
            while True:
                try:
                    chunk_size = shared_queue.get(block=False)
                    manual_progress += chunk_size
                    # print(manual_progress, "of", total_size)
                    progress.update(chunk_size)
                    # print(manual_progress, "of", total_size)

                    if manual_progress == total_size:
                        break
                except Exception as e:
                    # print("ex")
                    time.sleep(sleep_time)
                    continue


        results = async_results.get()
        assert async_results.successful(), "There was an uncaught error during hashing that we don't know how " \
                                           "to automatically fix. Sorry :("
        return results



def select(url, query, meta=None, raw=False, **kwargs):
    """Perform an S3 Select SQL query, return results as a Pandas DataFrame

    The data returned by Boto3 for S3 Select is fairly convoluted, to say the
    least.  This function returns the result as a dataframe instead.  It also
    performs the following actions, for convenience:

    * If quilt metadata is given, necessary info to handle the select query is
      pulled from the format metadata.
    * If no metadata is present, but the URL indicates an object with a known
      extension, the file format (and potentially compression) are determeined
      by that extension.
      * Extension may include a compresssion extension in cases where that is
        supported by AWS -- I.e, for queries on JSON or CSV files, .bz2 and
        .gz are supported.
      * Parquet files must not be compressed as a whole, and should not have
        a compression extension.  However, columnar GZIP and Snappy are
        transparently supported.

    Args:
        url(str):  S3 URL of the object to query
        query(str): An SQL query using the 'SELECT' directive. See examples at
            https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectSELECTContent.html
        meta: Quilt Object Metadata
        raw(bool):  True to return the raw Boto3 response object
        **kwargs:  s3_client.select() kwargs override.
            All kwargs specified passed to S3 client directly, overriding
            matching default/generated kwargs for `select_object_content()`.
            Note that this will also override the bucket and key specified in
            the URL if `Bucket` and `Key` are passed as kwargs.

    Returns: pandas.DataFrame | dict
        dict is returned if 'raw' is True or if OutputSerialization is set to
            something other than JSON Lines.

    """
    # We don't process any other kind of response at this time.
    output_serialization = {'JSON': {}}
    query_type = "SQL"  # AWS S3 doesn't currently support anything else.
    meta = meta if meta is not None else {}

    # Internal Format Name <--> S3 Format Name
    valid_s3_select_formats = {
        'parquet': 'Parquet',
        'json': 'JSON',
        'jsonl': 'JSON',
        'csv': 'CSV',
        }
    # S3 Format Name <--> S3-Acceptable compression types
    format_compression = {
        'Parquet': ['NONE'],  # even if column-level compression has been used.
        'JSON': ['NONE', 'BZIP2', 'GZIP'],
        'CSV': ['NONE', 'BZIP2', 'GZIP'],
        }
    # File extension <--> S3-Acceptable compression type
    # For compression type, when not specified in metadata.  Guess by extension.
    accepted_compression = {
        '.bz2': 'BZIP2',
        '.gz': 'GZIP'
        }
    # Extension <--> Internal Format Name
    # For file type, when not specified in metadata. Guess by extension.
    ext_formats = {
        '.parquet': 'parquet',
        '.json': 'json',
        '.jsonl': 'jsonl',
        '.csv': 'csv',
        '.tsv': 'csv',
        '.ssv': 'csv',
        }
    delims = {'.tsv': '\t', '.ssv': ';'}

    parsed_url = urlparse(url)
    bucket, path, version_id = parse_s3_url(parsed_url)

    # TODO: Use formats lib for this stuff
    # use metadata to get format and compression
    compression = None
    format = meta.get('target')
    if format is None:
        format = meta.get('format', {}).get('name')
        if format in ('bzip2', 'gzip'):
            compression = format.upper()
            format = meta.get('format', {}).get('contained_format', {}).get('name')

    # use file extensions to get compression info, if none is present
    exts = pathlib.Path(path).suffixes  # last of e.g. ['.periods', '.in', '.name', '.json', '.gz']
    if exts and not compression:
        if exts[-1].lower() in accepted_compression:
            compression = accepted_compression[exts.pop(-1)]   # remove e.g. '.gz'
    compression = compression if compression else 'NONE'

    # use remaining file extensions to get format info, if none is present
    csv_delim = None
    if exts and not format:
        ext = exts[-1].lower()    # last of e.g. ['.periods', '.in', '.name', '.json']
        if ext in ext_formats:
            format = ext_formats[ext]
            csv_delim = delims.get(ext)
            s3_format = valid_s3_select_formats[format]
            ok_compression = format_compression[s3_format]
            if compression not in ok_compression:
                raise QuiltException("Compression {!r} not valid for select on format {!r}: "
                                     "Expected {!r}".format(compression, s3_format, ok_compression))
    if not format:
        raise QuiltException("Unable to discover format for select on {!r}".format(url))

    # At this point, we have a known format and enough information to use it.
    s3_format = valid_s3_select_formats[format]

    # Create InputSerialization section if not user-specified.
    input_serialization = None
    if 'InputSerialization' not in kwargs:
        input_serialization = {'CompressionType': compression}
        format_spec = input_serialization.setdefault(s3_format, {})

        if s3_format == 'JSON':
            format_spec['Type'] = "LINES" if format == 'jsonl' else "DOCUMENT"
        elif s3_format == 'CSV':
            if csv_delim is not None:
                format_spec['FieldDelimiter'] = csv_delim

    # These are processed and/or default args.
    select_kwargs = dict(
        Bucket=bucket,
        Key=path,
        Expression=query,
        ExpressionType=query_type,
        InputSerialization=input_serialization,
        OutputSerialization=output_serialization,
    )
    # Include user-specified passthrough options, overriding other options
    select_kwargs.update(kwargs)

    s3_client = create_s3_client()
    response = s3_client.select_object_content(**select_kwargs)

    # we don't want multiple copies of large chunks of data hanging around.
    # ..iteration ftw.  It's what we get from amazon, anyways..
    def iter_chunks(resp):
        for item in resp['Payload']:
            chunk = item.get('Records', {}).get('Payload')
            if chunk is None:
                continue
            yield chunk

    def iter_lines(resp, delimiter):
        # S3 may break chunks off at any point, so we need to find line endings and handle
        # line breaks manually.
        # Note: this isn't reliable for CSV, because CSV may have a quoted line ending,
        # whereas line endings in JSONLines content will be encoded cleanly.
        lastline = ''
        for chunk in iterdecode(iter_chunks(resp), 'utf-8'):
            lines = chunk.split(delimiter)
            lines[0] = lastline + lines[0]
            lastline = lines.pop(-1)
            for line in lines:
                yield line + delimiter
        yield lastline

    if not raw:
        # JSON used for processed content as it doesn't have the ambiguity of CSV.
        if 'JSON' in select_kwargs["OutputSerialization"]:
            delimiter = select_kwargs['OutputSerialization']['JSON'].get('RecordDelimiter', '\n')
            reader = jsonlines.Reader(line.strip() for line in iter_lines(response, delimiter)
                                      if line.strip())
            # noinspection PyPackageRequirements
            from pandas import DataFrame   # Lazy import for slow module
            # !! if this response type is modified, update related docstrings on Bucket.select().
            return DataFrame.from_records(x for x in reader)
        # If there's some need, we could implement some other OutputSerialization format here.
        # If they've specified an OutputSerialization key we don't handle, just give them the
        # raw response.
    # !! if this response type is modified, update related docstrings on Bucket.select().
    return response