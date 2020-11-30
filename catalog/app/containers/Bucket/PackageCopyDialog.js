import * as R from 'ramda'
import { FORM_ERROR } from 'final-form'
import * as React from 'react'
import * as RF from 'react-final-form'
import * as M from '@material-ui/core'

import AsyncResult from 'utils/AsyncResult'
import * as APIConnector from 'utils/APIConnector'
import * as AWS from 'utils/AWS'
import * as Data from 'utils/Data'
import Delay from 'utils/Delay'
import Dropzone, { FilesStats, Overlay as DropzoneOverlay } from 'components/Dropzone'
import * as NamedRoutes from 'utils/NamedRoutes'
import { getBasename } from 'utils/s3paths'
// import { readableBytes } from 'utils/string'
import StyledLink from 'utils/StyledLink'
import tagged from 'utils/tagged'
import * as validators from 'utils/validators'

import * as PD from './PackageDialog'
import * as requests from './requests'

const getTotalProgress = R.pipe(
  R.values,
  R.reduce(
    (acc, { progress: p = {} }) => ({
      total: acc.total + (p.total || 0),
      loaded: acc.loaded + (p.loaded || 0),
    }),
    { total: 0, loaded: 0 },
  ),
  (p) => ({
    ...p,
    percent: p.total ? Math.floor((p.loaded / p.total) * 100) : 100,
  }),
)

const useFilesInputStyles = M.makeStyles((t) => ({
  root: {
    marginTop: t.spacing(3),
  },
}))

async function requestPackageCopy(
  req,
  { commitMessage, hash, initialName, meta, name, sourceBucket, targetBucket, workflow },
) {
  try {
    return req({
      endpoint: '/packages/promote',
      method: 'POST',
      body: {
        copy_data: true,
        message: commitMessage,
        meta: PD.getMetaValue(meta),
        name,
        parent: {
          top_hash: hash,
          registry: `s3://${sourceBucket}`,
          name: initialName,
        },
        registry: `s3://${targetBucket}`,
        workflow: PD.getWorkflowApiParam(workflow.slug),
      },
    })
  } catch (e) {
    return { [FORM_ERROR]: e.message || PD.ERROR_MESSAGES.MANIFEST }
  }
}

const filesInitialValue = { existing: [] }

function FilesInput({ input: { value: inputValue }, meta }) {
  const classes = useFilesInputStyles()

  const value = inputValue || filesInitialValue
  const error = meta.submitFailed && meta.error

  const files = value.existing.map(({ file }) => ({
    key: file.physicalKey,
    path: getBasename(decodeURIComponent(file.physicalKey)),
    size: file.size,
  }))

  // const totalSize = React.useMemo(() => value.reduce((sum, f) => sum + f.file.size, 0), [
  //   value,
  // ])

  // const warning = React.useMemo(
  //   () =>
  //     totalSize > PD.MAX_SIZE
  //       ? `Total file size exceeds recommended maximum of ${readableBytes(PD.MAX_SIZE)}`
  //       : null,
  //   [totalSize],
  // )

  // NOTE: User can't upload 1Gb, because Dropzone is disabled
  const warning = null

  return (
    <Dropzone
      className={classes.root}
      disabled
      error={error}
      files={files}
      overlayComponent={<DropzoneOverlay />}
      statsComponent={<FilesStats files={files} warning={warning} />}
      warning={warning}
      onDrop={R.always(files)}
    />
  )
}

function DialogTitle({ bucket }) {
  const { urls } = NamedRoutes.use()

  return (
    <M.DialogTitle>
      Promote package to{' '}
      <StyledLink target="_blank" to={urls.bucketOverview(bucket)}>
        {bucket}
      </StyledLink>{' '}
      bucket
    </M.DialogTitle>
  )
}

function DialogForm({
  close,
  hash,
  manifest,
  name: initialName,
  onSuccess,
  sourceBucket,
  targetBucket,
  workflowsConfig,
}) {
  const [uploads, setUploads] = React.useState({})

  const nameValidator = PD.useNameValidator()

  const initialMeta = React.useMemo(
    () => ({
      mode: 'kv',
      text: JSON.stringify(manifest.meta || {}),
    }),
    [manifest.meta],
  )

  const initialFiles = {
    existing: Object.values(manifest.entries).map((file) => ({
      file,
    })),
  }

  const req = APIConnector.use()

  const onSubmit = async ({ commitMessage, name, meta, workflow }) => {
    const res = await requestPackageCopy(req, {
      commitMessage,
      hash,
      initialName,
      meta,
      name,
      sourceBucket,
      targetBucket,
      workflow,
    })
    onSuccess({ name, hash: res.top_hash })
    return { [FORM_ERROR]: 'Error creating manifest' }
  }

  const totalProgress = React.useMemo(() => getTotalProgress(uploads), [uploads])

  return (
    <RF.Form onSubmit={onSubmit}>
      {({
        handleSubmit,
        submitting,
        submitFailed,
        error,
        submitError,
        hasValidationErrors,
        form,
        values,
      }) => (
        <>
          <DialogTitle bucket={targetBucket} />
          <M.DialogContent style={{ paddingTop: 0 }}>
            <form onSubmit={handleSubmit}>
              <RF.Field
                component={PD.Field}
                name="name"
                label="Name"
                placeholder="Enter a package name"
                validate={validators.composeAsync(
                  validators.required,
                  nameValidator.validate,
                )}
                validateFields={['name']}
                errors={{
                  required: 'Enter a package name',
                  invalid: 'Invalid package name',
                }}
                margin="normal"
                fullWidth
                initialValue={initialName}
              />

              <RF.Field
                component={PD.Field}
                name="commitMessage"
                label="Commit message"
                placeholder="Enter a commit message"
                validate={validators.required}
                validateFields={['commitMessage']}
                errors={{
                  required: 'Enter a commit message',
                }}
                fullWidth
                margin="normal"
              />

              <RF.Field
                component={FilesInput}
                name="files"
                validate={validators.nonEmpty}
                validateFields={['files']}
                errors={{
                  nonEmpty: 'Add files to create a package',
                }}
                uploads={uploads}
                setUploads={setUploads}
                isEqual={R.equals}
                initialValue={initialFiles}
              />

              <PD.SchemaFetcher
                schemaUrl={R.pathOr('', ['schema', 'url'], values.workflow)}
              >
                {AsyncResult.case({
                  Ok: ({ responseError, schema, validate }) => (
                    <RF.Field
                      component={PD.MetaInput}
                      name="meta"
                      bucket={targetBucket}
                      schema={schema}
                      schemaError={responseError}
                      validate={validate}
                      validateFields={['meta']}
                      isEqual={R.equals}
                      initialValue={initialMeta}
                    />
                  ),
                  _: () => <PD.MetaInputSkeleton />,
                })}
              </PD.SchemaFetcher>

              <RF.Field
                component={PD.WorkflowInput}
                name="workflow"
                workflowsConfig={workflowsConfig}
                initialValue={PD.defaultWorkflowFromConfig(workflowsConfig)}
                validateFields={['meta', 'workflow']}
              />

              <input type="submit" style={{ display: 'none' }} />
            </form>
          </M.DialogContent>
          <M.DialogActions>
            {submitting && (
              <Delay ms={200} alwaysRender>
                {(ready) => (
                  <M.Fade in={ready}>
                    <M.Box flexGrow={1} display="flex" alignItems="center" pl={2}>
                      <M.CircularProgress
                        size={24}
                        variant={
                          totalProgress.percent < 100 ? 'determinate' : 'indeterminate'
                        }
                        value={
                          totalProgress.percent < 100
                            ? totalProgress.percent * 0.9
                            : undefined
                        }
                      />
                      <M.Box pl={1} />
                      <M.Typography variant="body2" color="textSecondary">
                        {totalProgress.percent < 100
                          ? 'Uploading files'
                          : 'Writing manifest'}
                      </M.Typography>
                    </M.Box>
                  </M.Fade>
                )}
              </Delay>
            )}

            {!submitting && (!!error || !!submitError) && (
              <M.Box flexGrow={1} display="flex" alignItems="center" pl={2}>
                <M.Icon color="error">error_outline</M.Icon>
                <M.Box pl={1} />
                <M.Typography variant="body2" color="error">
                  {error || submitError}
                </M.Typography>
              </M.Box>
            )}

            <M.Button onClick={close} disabled={submitting}>
              Cancel
            </M.Button>
            <M.Button
              onClick={handleSubmit}
              variant="contained"
              color="primary"
              disabled={submitting || (submitFailed && hasValidationErrors)}
            >
              Push
            </M.Button>
          </M.DialogActions>
        </>
      )}
    </RF.Form>
  )
}

function DialogError({ bucket, error, onCancel }) {
  const { urls } = NamedRoutes.use()

  return (
    <PD.DialogError
      error={error}
      title={
        <>
          Promote package to{' '}
          <StyledLink target="_blank" to={urls.bucketOverview(bucket)}>
            {bucket}
          </StyledLink>{' '}
          bucket
        </>
      }
      onCancel={onCancel}
    />
  )
}

function DialogLoading({ bucket, onCancel }) {
  const { urls } = NamedRoutes.use()

  return (
    <PD.DialogLoading
      title={
        <>
          Promote package to{' '}
          <StyledLink target="_blank" to={urls.bucketOverview(bucket)}>
            {bucket}
          </StyledLink>{' '}
          bucket
        </>
      }
      onCancel={onCancel}
    />
  )
}

const DialogState = tagged(['Loading', 'Error', 'Form', 'Success'])

export default function PackageCopyDialog({
  sourceBucket,
  targetBucket,
  name,
  hash,
  onClose,
}) {
  const s3 = AWS.S3.use()

  const [success, setSuccess] = React.useState(false)

  const manifestData = Data.use(requests.loadManifest, {
    s3,
    bucket: sourceBucket,
    name,
    hash,
  })

  const workflowsData = Data.use(requests.workflowsList, { s3, bucket: targetBucket })

  const state = React.useMemo(() => {
    if (success) return DialogState.Success(success)
    return workflowsData.case({
      Ok: (workflowsConfig) =>
        manifestData.case({
          Ok: (manifest) => DialogState.Form({ manifest, workflowsConfig }),
          Err: DialogState.Error,
          _: DialogState.Loading,
        }),
      Err: DialogState.Error,
      _: DialogState.Loading,
    })
  }, [success, workflowsData, manifestData])

  const stateCase = React.useCallback((cases) => DialogState.case(cases, state), [state])

  return (
    <M.Dialog open onClose={onClose} fullWidth scroll="body">
      {stateCase({
        Error: (e) => <DialogError bucket={targetBucket} onClose={onClose} error={e} />,
        Loading: () => <DialogLoading bucket={targetBucket} onCancel={onClose} />,
        Form: (props) => (
          <DialogForm
            {...{
              close: onClose,
              hash,
              name,
              onSuccess: setSuccess,
              sourceBucket,
              targetBucket,
              ...props,
            }}
          />
        ),
        Success: (props) => (
          <PD.DialogSuccess
            bucket={targetBucket}
            name={props.name}
            hash={props.hash}
            onClose={onClose}
          />
        ),
      })}
    </M.Dialog>
  )
}
