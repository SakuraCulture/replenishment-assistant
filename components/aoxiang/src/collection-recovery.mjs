// An accepted task may outlive a local collection. Its identity is never discarded.
export function resumableTask(attempt){
  return !!(attempt?.runId&&typeof attempt.taskId==='string'&&attempt.taskId&&
    ['ACCEPTED','RUNNING'].includes(attempt.stage)&&!attempt.additionalTaskIds?.length);
}
