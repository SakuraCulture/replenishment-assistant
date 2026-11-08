// Cooperative page ownership handoff. No process teardown and no replay/resume.
export async function releaseSession({runtime,commands,setup,template,purchase,receiptSync,bindingCheck}){
  for(const controller of [purchase,receiptSync,bindingCheck])if(controller.busy)controller.pause(controller.job.id);
  for(const record of commands.list())if(['QUEUED','RUNNING'].includes(record.state))runtime.pause(record.id);
  if(template.busy)template.cancel(template.job.id);
  if(setup.busy)await setup.cancel(setup.job.id);
  else await setup.handoffLogin?.();
  return {idle:!runtime.active&&!setup.busy&&!template.busy&&!purchase.busy&&!receiptSync.busy&&!bindingCheck.busy};
}
