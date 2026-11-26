import {CatalogError} from './common.mjs';

// Keep business/identity errors distinct from a missing UI element, even for
// older callers that rely on the existing public error code.
export function locationError(message, code='JST_PAGE_LOCATION') {
  const error=new CatalogError(code,message);
  error.pageLocation=true;
  return error;
}

export function browserFault(error,page) {
  if (error?.connectionLost||['JST_CONNECTION_LOST','CONNECTION_NOT_ESTABLISHED','BROWSER_SETUP','ECONNRESET','ECONNREFUSED'].includes(error?.code))
    return 'connection';
  const locator=error?.pageLocation||['CONTROL_NOT_READY','CONTROL_AMBIGUOUS','JST_PAGE_LOCATION'].includes(error?.code);
  if (error?.code&&!locator&&error.code!=='DOWNLOAD_FAILED') return null;
  if (page?.isClosed?.()||page?.context?.()?.browser?.()?.isConnected?.()===false||
      /target page, context or browser has been closed|target closed|browser has been closed|browser disconnected|net::ERR_(CONNECTION_(RESET|CLOSED|REFUSED)|INTERNET_DISCONNECTED|NETWORK_CHANGED)/i.test(error?.message||''))
    return 'connection';
  if (locator||error?.name==='TimeoutError'&&/^(?:locator\.|frame\.(?:waitFor|goto)|page\.(?:waitFor|goto))/.test(error.message))
    return 'location';
  return null;
}
