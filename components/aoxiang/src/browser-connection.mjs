// Observation only: never connects, navigates, verifies login or restarts a worker.
export const CONNECTION_STATES=new Set(['NOT_CONNECTED','CONNECTING','CONNECTED','DISCONNECTED','UNKNOWN']);
export function browserConnectionState(bridge){
  return bridge.browser?.isConnected()&&bridge.page&&!bridge.page.isClosed()?'CONNECTED':'DISCONNECTED';
}
export function watchBrowserConnection(bridge,publish){
  const browser=bridge.browser,page=bridge.page;
  const update=()=>publish(browserConnectionState(bridge));
  browser.on('disconnected',update);page.on('close',update);update();
  return ()=>{browser.off('disconnected',update);page.off('close',update);};
}
export function clientConnectionState(client){
  if(!client)return 'NOT_CONNECTED';
  if(client.closed)return 'DISCONNECTED';
  return CONNECTION_STATES.has(client.connectionState)?client.connectionState:'UNKNOWN';
}
