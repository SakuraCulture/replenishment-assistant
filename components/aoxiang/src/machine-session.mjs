// Cooperative page ownership handoff. Regular collection commands own their queue.
export async function releaseSession({runtime,commands,setup,template,purchase,receiptSync,bindingCheck}){
  for(const controller of [purchase,receiptSync,bindingCheck])if(controller.busy)controller.pause(controller.job.id);
  const otherCollections=commands.list().filter(record=>!record.durable_queue);
  for(const record of otherCollections)if(['QUEUED','RUNNING'].includes(record.state))runtime.pause(record.id);
  if(template.busy)template.cancel(template.job.id);
  if(setup.busy)await setup.cancel(setup.job.id);
  else if(!runtime.active)await setup.handoffLogin?.();
  const otherActive=otherCollections.some(record=>record.id===runtime.active?.id);
  return {idle:!otherActive&&!setup.busy&&!template.busy&&!purchase.busy&&!receiptSync.busy&&!bindingCheck.busy};
}
