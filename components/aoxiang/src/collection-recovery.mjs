// An accepted task may outlive a local collection. Its identity is never discarded.
export function resumableTask(attempt){
  return !!(attempt?.runId&&typeof attempt.taskId==='string'&&attempt.taskId&&
    ['ACCEPTED','RUNNING'].includes(attempt.stage)&&!attempt.additionalTaskIds?.length);
}

export function resumableReview(state,item,independent=false){
  const entries=state.getItems(item.run_id);
  if(entries.length!==1)return false;
  const entry=entries[0],attempt=state.getAttempt(entry.attemptId);
  if(!attempt){
    // rc18 could mark an unsent item for review solely because another batch existed.
    return independent&&entry.stage==='PLANNED'&&item.error?.code==='UNRESOLVED_TASK'&&
      !state.db.prepare('SELECT 1 FROM attempts WHERE item_id=?').get(entry.id);
  }
  return !attempt.additionalTaskIds?.length&&
    (attempt.stage==='CANCELLED_BEFORE_SUBMIT'||attempt.taskId&&
      ['ACCEPTED','RUNNING','FINISHED','DOWNLOADED','VALIDATED','PUBLISHING','SUCCEEDED'].includes(attempt.stage));
}
