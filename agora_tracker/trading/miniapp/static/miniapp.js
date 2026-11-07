const CHAIN = 99901;
const CHAIN_HEX = '0x1863d';
const ROUTER = '0x3c5caa33d02c78935fc682a8eb295f0e67b93ced';
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const UINT = /^(0|[1-9][0-9]{0,77})$/;
const allowedMethods = new Set(['eth_requestAccounts', 'eth_accounts', 'eth_chainId', 'wallet_switchEthereumChain', 'wallet_addEthereumChain']);
const $ = id => document.getElementById(id);
const parts = new URLSearchParams(location.hash.slice(1));
let intent = parts.getAll('intent').length === 1 ? parts.get('intent') : null;
if (!/^[A-Za-z0-9_-]{16}$/.test(intent || '')) intent = null;
history.replaceState(null, '', location.pathname + location.search);
let context = null, account = null, provider = null, quote = null;
let generation = 0, verified = false, telegramAuthenticated = false, clockOffset = 0, ended = false;
const states = {
  not_authenticated: ['Authentication required', 'Authenticate from your private Telegram alert before connecting a wallet.'],
  wallet_disconnected: ['Connect your wallet', 'Connecting an account does not prove ownership. Verification is a separate step.'],
  wallet_unverified: ['Wallet not verified', 'This account is connected, but ownership is not verified for this alert session.'],
  verified_wallet: ['Wallet verified', 'Choose an amount and request a read-only quote. No signing is enabled.'],
  quote_loading: ['Loading quote', 'Reading the supported pool and wallet snapshot. Nothing is sent to your wallet.'],
  quote_ready: ['Quote ready', 'Review the exact unsigned payload before any later signing milestone.'],
  quote_stale: ['Quote expired', 'Refresh the quote. The previous unsigned review is invalid.'],
  unsupported_route: ['Quote unavailable', 'This token or amount has no supported preview. Use the verified external DEX link.'],
  unsigned_review: ['Unsigned review · signing disabled', 'No transaction, approval or signature request is available in this milestone.'],
  expired: ['Alert session expired', 'Open a fresh Mini App link from your private alert panel.'],
  cancelled: ['Preview cancelled', 'Nothing was signed, approved or broadcast. Reopen the alert for a fresh session.'],
};
const now = () => Date.now() / 1000 + clockOffset;
const expired = () => ended || !context || now() >= context.expires_at;
function status(state, message) {
  if (ended && !['cancelled', 'expired'].includes(state)) return;
  $('wallet-state').textContent = ended ? 'Session closed' : verified ? 'Verified' : account ? 'Ownership unverified' : 'Disconnected';
  const text = states[state];
  $('app').dataset.state = state;
  $('status-title').textContent = text[0];
  $('status-message').textContent = message || text[1];
  const enabled = verified && !expired() && state !== 'quote_loading';
  for (const id of ['amount', 'slippage', 'refresh']) $(id).disabled = !enabled;
  for (const button of $('presets').querySelectorAll('button')) button.disabled = !enabled;
  $('review').disabled = !quote || !enabled || now() >= quote.expires_at;
  $('connect').disabled = !telegramAuthenticated || ended || (context && now() >= context.expires_at);
}
function clearReview() {
  quote = null;
  $('quote-card').hidden = true;
  $('review-card').hidden = true;
  $('review').disabled = true;
  $('calldata').value = '';
}
function invalidate(state, message) { generation += 1; clearReview(); status(state, message); }
function fail(error) {
  const state = ['not_authenticated', 'wallet_unverified', 'unsupported_route'].includes(error.code) ? error.code : 'unsupported_route';
  if (state === 'wallet_unverified' || state === 'not_authenticated') verified = false;
  if (state === 'not_authenticated') telegramAuthenticated = false;
  invalidate(state, error.message || 'Preview unavailable. No signing action is enabled.');
}
function secureLink() { return `${location.origin}${location.pathname}#intent=${encodeURIComponent(intent || '')}`; }
function fallback(message) {
  $('fallback').hidden = false;
  $('external-browser').href = secureLink();
  if (message) $('copy-status').textContent = message;
}
async function api(action, extra = {}) {
  const response = await fetch(`/api/${action}`, {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, cache: 'no-store',
    body: JSON.stringify({intent, init_data: window.Telegram?.WebApp?.initData || '', wallet: account, ...extra})});
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.message || 'Preview unavailable'); error.code = data.error; throw error; }
  if (data.signing_enabled !== false) throw new Error('Invalid preview capability. Signing remains disabled.');
  return data;
}
function adoptContext(data) {
  if (data.intent !== intent || data.chain_id !== CHAIN || !ADDRESS.test(data.token) || !['buy', 'sell'].includes(data.side) ||
      !Number.isFinite(data.expires_at) || !Number.isFinite(data.server_time)) throw new Error('Invalid server-issued alert context');
  context = data;
  clockOffset = data.server_time - Date.now() / 1000;
  telegramAuthenticated = data.auth?.telegram_authenticated === true;
  verified = telegramAuthenticated && data.auth?.wallet_verified === true && typeof data.auth.wallet === 'string' && data.auth.wallet.toLowerCase() === account;
  $('fixture').hidden = data.auth?.fixture !== true;
  $('token').textContent = data.token;
  $('side').textContent = `${data.side.toUpperCase()} · HYPE ${data.side === 'buy' ? '→ TOKEN' : '← TOKEN'}`;
  $('source').textContent = `Original alert: chat ${data.source_chat}, message ${data.source_message}`;
  const external = new URL(data.external_url);
  if (external.protocol !== 'https:' || external.hostname !== 'www.agoradex.app' || external.pathname !== `/token/${data.token}`) throw new Error('Invalid external DEX link');
  $('dex').href = external.href; $('dex').hidden = false;
  $('amount-label').textContent = data.side === 'buy' ? 'Custom input · HYPE' : 'Custom sell · % of verified token balance';
  const values = data.side === 'buy' ? ['0.1', '0.5', '1'] : ['25', '50', '100'];
  if (!$('presets').children.length) {
    $('amount').value = values[0];
    for (const value of values) {
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = value + (data.side === 'buy' ? ' HYPE' : '%');
      button.setAttribute('aria-pressed', value === values[0] ? 'true' : 'false');
      button.addEventListener('click', () => { $('amount').value = value; selectionChanged();
        for (const sibling of $('presets').children) sibling.setAttribute('aria-pressed', String(sibling === button)); });
      $('presets').append(button);
    }
  }
}
async function authenticate() {
  if (!intent || ended) return invalidate('expired');
  const turn = ++generation; clearReview();
  try {
    const data = await api('session');
    if (turn !== generation) return;
    adoptContext(data);
    if (expired()) return invalidate('expired');
    $('wallet-state').textContent = verified ? 'Verified' : account ? 'Ownership unverified' : 'Disconnected';
    status(verified ? 'verified_wallet' : account ? 'wallet_unverified' : 'wallet_disconnected');
  } catch (error) { if (turn === generation) { telegramAuthenticated = false; verified = false; fail(error); } }
}
async function requestWallet(method, params) {
  if (!allowedMethods.has(method)) throw new Error('Wallet operation is disabled in C1');
  return provider.request({method, ...(params ? {params} : {})});
}
function detach() {
  if (provider) for (const [name, listener] of [['accountsChanged', accountChanged], ['chainChanged', chainChanged], ['disconnect', disconnected]]) provider.removeListener(name, listener);
  provider = null;
}
function disconnected() { account = null; verified = false; $('wallet-address').textContent = 'No wallet connected'; invalidate('wallet_disconnected'); }
function accountChanged() { verified = false; account = null; $('wallet-address').textContent = 'Account changed. Reconnect and verify.'; invalidate('wallet_disconnected'); }
function chainChanged() { verified = false; invalidate('wallet_unverified', 'Wallet network changed. Reconnect on chain 99901 and verify again.'); }
async function connect() {
  if (!isSecureContext) return fallback('Wallet connections require a secure HTTPS context.');
  if (!telegramAuthenticated || expired()) return invalidate('not_authenticated');
  detach();
  const candidate = window.ethereum;
  if (!candidate || typeof candidate.request !== 'function' || typeof candidate.on !== 'function' || typeof candidate.removeListener !== 'function') {
    return fallback('No compatible EIP-1193 provider detected. Use an external wallet browser.');
  }
  provider = candidate;
  for (const [name, listener] of [['accountsChanged', accountChanged], ['chainChanged', chainChanged], ['disconnect', disconnected]]) provider.on(name, listener);
  clearReview(); verified = false;
  try {
    const accounts = await requestWallet('eth_requestAccounts');
    if (!Array.isArray(accounts) || !ADDRESS.test(accounts[0] || '') || /^0x0{40}$/i.test(accounts[0])) throw new Error('Wallet did not expose a valid account');
    let chain = await requestWallet('eth_chainId');
    if (chain !== CHAIN_HEX) {
      try { await requestWallet('wallet_switchEthereumChain', [{chainId: CHAIN_HEX}]); }
      catch (error) {
        if (error.code !== 4902) throw error;
        await requestWallet('wallet_addEthereumChain', [{chainId: CHAIN_HEX, chainName: 'Elysium',
          nativeCurrency: {name: 'HYPE', symbol: 'HYPE', decimals: 18}, rpcUrls: ['https://rpc.agoradex.app']}]);
        await requestWallet('wallet_switchEthereumChain', [{chainId: CHAIN_HEX}]);
      }
      chain = await requestWallet('eth_chainId');
    }
    if (chain !== CHAIN_HEX) throw new Error('Wallet is on the wrong chain. Chain 99901 is required.');
    const current = await requestWallet('eth_accounts');
    if (!Array.isArray(current) || current[0]?.toLowerCase() !== accounts[0].toLowerCase()) throw new Error('Account changed during connection. Reconnect.');
    if (ended) return;
    account = current[0].toLowerCase(); $('wallet-address').textContent = account;
    $('fallback').hidden = true;
    await authenticate();
  } catch (error) { verified = false; invalidate('wallet_unverified', error.code === 4001 ? 'Wallet connection or network switch was declined.' : error.message); fallback(); }
}
function selectionChanged() { invalidate(verified ? 'verified_wallet' : 'wallet_unverified'); }
function formatUnits(value, decimals) {
  if (typeof value !== 'string' || !UINT.test(value) || !Number.isInteger(decimals) || decimals < 0 || decimals > 36) return 'Unavailable';
  const digits = value.padStart(decimals + 1, '0');
  return decimals ? digits.slice(0, -decimals) + ('.' + digits.slice(-decimals)).replace(/\.?0+$/, '') : value;
}
function details(element, entries) {
  element.replaceChildren();
  for (const [name, value] of entries) {
    const term = document.createElement('dt'), text = document.createElement('dd');
    term.textContent = name; text.textContent = String(value); element.append(term, text);
  }
}
function validateQuote(q) {
  if (expired() || q.intent_id !== intent || q.chain_id !== CHAIN || q.token !== context.token || q.side !== context.side ||
      q.wallet !== account || q.route?.kind !== 'canonical-v2' || q.route.target !== ROUTER || now() >= q.expires_at ||
      !Number.isFinite(q.expires_at) || !Number.isInteger(q.token_decimals) || q.token_decimals < 0 || q.token_decimals > 36) throw new Error('Stale or mismatched unsigned preview');
  for (const field of ['amount_in_wei', 'amount_out_wei', 'min_out_wei', 'balance_wei', 'native_balance_wei']) if (typeof q[field] !== 'string' || !UINT.test(q[field])) throw new Error('Invalid financial amount');
  if (!q.transaction || q.transaction.from !== account || q.transaction.to !== ROUTER || typeof q.transaction.data !== 'string' || !/^0x[0-9a-fA-F]+$/.test(q.transaction.data)) throw new Error('Invalid unsigned transaction details');
}
async function refresh() {
  if (!verified || expired()) return invalidate('wallet_unverified');
  const turn = ++generation; clearReview(); status('quote_loading');
  try {
    const data = await api('quote', {amount: $('amount').value.trim(), slippage_bps: Number($('slippage').value)});
    if (turn !== generation || ended) return;
    adoptContext(data.context); if (!verified) throw new Error('Wallet verification changed'); validateQuote(data.quote); quote = data.quote;
    const inputDecimals = context.side === 'buy' ? 18 : quote.token_decimals;
    const outputDecimals = context.side === 'buy' ? quote.token_decimals : 18;
    details($('quote-details'), [
      ['Input', formatUnits(quote.amount_in_wei, inputDecimals)], ['Expected pool output', formatUnits(quote.amount_out_wei, outputDecimals)],
      ['Minimum output', formatUnits(quote.min_out_wei, outputDecimals)], ['Available input', formatUnits(quote.balance_wei, inputDecimals)],
      ['HYPE balance', formatUnits(quote.native_balance_wei, 18)], ['Pool fee (included)', `${quote.pool_fee_bps} bps`],
      ['Gas estimate (units)', quote.gas_estimate ?? 'Unavailable'], ['Gas price (wei)', quote.gas_price_wei ?? 'Unavailable'],
      ['Slippage limit', $('slippage').value + ' bps'], ['Gas reserve · HYPE', quote.gas_wei === null ? 'Unavailable — allowance required' : formatUnits(quote.gas_wei, 18)],
      ['Finite allowance required (raw)', quote.approval_required_wei], ['Approval spender', quote.approval_spender || 'None'],
      ['Price impact', `${quote.price_impact_bps} bps`], ['Route', quote.route.kind], ['Pool', quote.pool],
      ['Simulation', quote.simulation_status === 'success' ? 'Minimum enforced at snapshot; future fills not guaranteed' : 'CONDITIONAL — missing allowance and reverted simulation; cause not established'],
      ['Observed at', new Date(quote.created_at * 1000).toISOString()], ['Deadline', new Date(quote.deadline * 1000).toISOString()], ['Snapshot block', quote.block_number],
    ]);
    $('quote-card').hidden = false; $('refresh').textContent = 'Refresh read-only quote'; status('quote_ready');
  } catch (error) { if (turn === generation) fail(error); }
}
async function review() {
  const turn = generation;
  try {
    validateQuote(quote);
    const chain = await requestWallet('eth_chainId'), accounts = await requestWallet('eth_accounts');
    if (turn !== generation) return;
    if (chain !== CHAIN_HEX || accounts[0]?.toLowerCase() !== account) return accountChanged();
    validateQuote(quote);
    details($('review-details'), [['Chain', CHAIN], ['Token', context.token], ['From', quote.transaction.from], ['To', quote.transaction.to],
      ['Native value (hex)', quote.transaction.value], ['Minimum output (raw)', quote.min_out_wei], ['Deadline', new Date(quote.deadline * 1000).toISOString()]]);
    $('calldata').value = quote.transaction.data; $('review-card').hidden = false; status('unsigned_review');
    $('review-card').scrollIntoView({block: 'nearest'});
  } catch (error) { invalidate('quote_stale', 'The unsigned review is stale or invalid. Refresh and verify again.'); }
}
async function cancel() {
  generation += 1; clearReview(); verified = false; ended = true; detach(); clearInterval(timer); status('cancelled');
  try { if (intent && telegramAuthenticated) await api('cancel'); } catch (_) { /* local cancellation always succeeds */ }
}
$('authenticate').addEventListener('click', authenticate);
$('connect').addEventListener('click', connect);
$('refresh').addEventListener('click', refresh);
$('review').addEventListener('click', review);
$('cancel').addEventListener('click', cancel);
$('amount').addEventListener('input', selectionChanged);
$('slippage').addEventListener('change', selectionChanged);
$('copy-link').addEventListener('click', async () => { try { await navigator.clipboard.writeText(secureLink()); $('copy-status').textContent = 'Secure link copied. Authentication is still required in the other browser.'; } catch (_) { $('copy-status').textContent = secureLink(); } });
$('external-browser').addEventListener('click', event => { if (window.Telegram?.WebApp?.openLink) { event.preventDefault(); window.Telegram.WebApp.openLink(secureLink()); } });
const timer = setInterval(() => {
  if (!context || ended) return;
  $('intent-expiry').textContent = `Alert session: ${Math.max(0, Math.ceil(context.expires_at - now()))} seconds remaining`;
  if (now() >= context.expires_at) { ended = true; verified = false; detach(); clearInterval(timer); return invalidate('expired'); }
  if (quote && now() >= quote.expires_at) invalidate('quote_stale');
  else if (quote) $('quote-age').textContent = `${Math.max(0, Math.floor(now() - quote.created_at))} s old`;
}, 1000);
function cleanup() { detach(); clearInterval(timer); generation += 1; clearReview(); verified = false; }
window.addEventListener('pagehide', cleanup, {once: true});
window.addEventListener('pageshow', event => { if (event.persisted) { ended = true; invalidate('expired', 'App was restored. Reopen a fresh private alert session.'); } });
document.addEventListener('visibilitychange', () => { if (document.hidden && (quote || verified)) { verified = false; invalidate('wallet_unverified', 'App activity changed. Reconnect and verify before another review.'); } });
function telegramReady() { window.Telegram?.WebApp?.ready?.(); }
$('telegram-sdk').addEventListener('load', () => { telegramReady(); if (intent && !ended && !telegramAuthenticated) authenticate(); }, {once: true}); telegramReady();
if (intent) authenticate(); else status('not_authenticated', 'Missing or invalid intent. Open a fresh link from your private alert panel.');
