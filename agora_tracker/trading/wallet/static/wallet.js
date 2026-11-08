'use strict';
// Wallet-managed order page (Stage C2). The page asks the user's OWN wallet (EIP-1193) to send the
// exact request the server prepared, after re-checking the parts that matter. Nothing here signs,
// holds a key, or retries a send. All dynamic text is set with textContent.
const CHAIN_HEX = '0x1863d';
const ROUTER = '0x3c5caa33d02c78935fc682a8eb295f0e67b93ced';
const UNLIMITED_FLOOR = 2n ** 128n;
const ADDRESS = /^0x[0-9a-f]{40}$/;
const TXHASH = /^0x[0-9a-f]{64}$/;
const $ = id => document.getElementById(id);
const parts = new URLSearchParams(location.hash.slice(1));
const intent = parts.get('intent');
const planId = parts.get('plan');
history.replaceState(null, '', location.pathname + location.search);
let account = null, order = null, current = null, sending = false, sent = false;

function say(text) { $('status').textContent = text; }
function fill(dl, rows) {
  dl.replaceChildren();
  for (const [k, v] of rows) {
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = k; dd.textContent = String(v);
    dl.append(dt, dd);
  }
}
async function api(name, extra) {
  const initData = (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) || '';
  const body = JSON.stringify({intent, init_data: initData, wallet: account, ...extra});
  const r = await fetch('/wallet/api/' + name, {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json'}, body});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.message || 'refused'); e.code = data.error || 'refused'; throw e; }
  return data;
}
function provider() { return window.ethereum && typeof window.ethereum.request === 'function' ? window.ethereum : null; }

async function connect() {
  const p = provider();
  if (!p) { say('No wallet found in this browser. Open this page in your wallet\'s browser.'); return; }
  const accounts = await p.request({method: 'eth_requestAccounts'});
  account = String(accounts[0] || '').toLowerCase();
  if (!ADDRESS.test(account)) { say('Wallet returned no usable account.'); account = null; return; }
  let chain = await p.request({method: 'eth_chainId'});
  if (String(chain).toLowerCase() !== CHAIN_HEX) {
    try { await p.request({method: 'wallet_switchEthereumChain', params: [{chainId: CHAIN_HEX}]}); }
    catch (e) { say('Switch your wallet to the Agora chain (' + CHAIN_HEX + ') to continue.'); return; }
    chain = await p.request({method: 'eth_chainId'});
  }
  $('wallet-account').textContent = account;
  $('wallet-chain').textContent = String(chain);
  if (String(chain).toLowerCase() !== CHAIN_HEX) { say('Wrong chain; nothing was requested.'); return; }
  $('order-card').hidden = false; $('create').disabled = !planId || !intent;
  say('Wallet connected. Ownership is verified by the server, not by this page.');
}

function showOrder(o) {
  order = o;
  fill($('order-fields'), [['Order', o.id], ['State', o.state], ['Side', o.side], ['Token', o.token],
    ['Amount in (wei)', o.amount_in_wei], ['Min out (wei)', o.min_out_wei],
    ['Slippage (bps)', o.slippage_bps], ['Deadline', new Date(o.deadline * 1000).toISOString()]]);
  $('cancel').disabled = !['created', 'quote_review', 'requires_approval'].includes(o.state);
}

// Independent re-check of what the server asks the wallet to do. It cannot prove the server honest,
// but it makes a tampered response (wrong target/spender/sender/unlimited amount) fail loudly.
function verifyRequest(req) {
  const tx = req && req.eip1193 && req.eip1193.params && req.eip1193.params[0];
  if (!tx || req.eip1193.method !== 'eth_sendTransaction') return 'malformed request';
  if (tx.from !== account || req.sender !== account) return 'request is not for your connected account';
  if (req.chain_hex !== CHAIN_HEX) return 'wrong chain';
  if (!ADDRESS.test(tx.to) || !/^0x([0-9a-f]{2})+$/.test(tx.data)) return 'malformed target or data';
  if (req.kind === 'approve' || req.kind === 'reset') {
    if (!tx.data.startsWith('0x095ea7b3') || tx.data.length !== 10 + 128) return 'not an exact approve call';
    const spender = '0x' + tx.data.slice(10 + 24, 10 + 64);
    const amount = BigInt('0x' + tx.data.slice(10 + 64));
    if (spender !== ROUTER || req.spender !== ROUTER) return 'approval spender is not the Agora router';
    if (amount >= UNLIMITED_FLOOR) return 'approval is not finite';
    if (String(amount) !== req.amount_wei) return 'approval amount differs from the request';
    if (req.kind === 'reset' && amount !== 0n) return 'reset must be zero';
    if (req.kind === 'approve' && (amount === 0n || amount > BigInt(order.amount_in_wei))) return 'approval exceeds the order amount';
    if (tx.to !== req.token || tx.value !== '0x0') return 'approval target or value is wrong';
  } else if (req.kind === 'swap') {
    if (tx.to !== ROUTER) return 'swap target is not the Agora router';
  } else return 'unknown request kind';
  if (!tx.gas || !tx.gasPrice) return 'request has no explicit gas caps';
  return null;
}

function showRequest(req) {
  current = req; sent = false;
  const kinds = {approve: 'Finite token approval', reset: 'Reset approval to zero', swap: 'Swap'};
  $('request-title').textContent = kinds[req.kind] || 'Wallet request';
  fill($('request-fields'), [['To', req.to], ['Spender', req.spender || '—'],
    ['Amount (wei)', req.amount_wei || '—'], ['Value (wei)', req.value_wei],
    ['Gas limit', req.gas_limit], ['Max gas fee (wei)', req.max_gas_fee_wei],
    ['Expires', new Date(req.expires_at * 1000).toISOString()], ['Digest', req.digest]]);
  const problem = verifyRequest(req);
  $('request-warning').textContent = problem ? 'Refusing to send: ' + problem : '';
  $('send').disabled = Boolean(problem);
  $('request-card').hidden = false;
}

async function createOrder() {
  $('create').disabled = true;
  try {
    const out = await api('order', {plan_id: planId});
    showOrder(out.order);
    const p = await api('prompt', {order_id: out.order.id, plan_id: planId});
    if (p.status === 'satisfied') { say('Allowance already sufficient. Request a fresh quote to continue.'); return; }
    showRequest(p.request);
    say('Review the request, then send it to your wallet.');
  } catch (e) { say('Refused: ' + (e.code || 'error')); }
}

async function send() {
  if (sending || sent || !current) return;           // single flight; never a second send
  sending = true; $('send').disabled = true;
  const req = current;
  let outcome, hash = null, reason = null;
  try {
    const result = await provider().request(req.eip1193);
    if (typeof result === 'string' && TXHASH.test(result)) { outcome = 'hash'; hash = result; }
    else { outcome = 'error'; reason = 'unspecified'; }
  } catch (e) {
    if (e && e.code === 4001) { outcome = 'rejected'; reason = 'user_rejected'; }
    else { outcome = 'error'; reason = e && e.code === 4900 ? 'disconnected' : 'unspecified'; }
  }
  sent = true;                                         // the wallet has answered: no more prompts
  try {
    const out = await api('report', {request_id: req.id, outcome, tx_hash: hash, reason});
    say(out.message || ('Recorded: ' + out.status));
    if (order) { const s = await api('status', {order_id: order.id}); showOrder(s.order); }
  } catch (e) { say('Your wallet answered (' + outcome + ') but the server could not record it: ' + (e.code || 'error') + '. Do not retry; check your wallet history.'); }
  sending = false;
}

$('connect').addEventListener('click', () => connect().catch(() => say('Wallet connection failed.')));
$('create').addEventListener('click', () => createOrder());
$('send').addEventListener('click', () => send());
$('cancel').addEventListener('click', async () => {
  $('cancel').disabled = true;
  try { showOrder((await api('cancel', {order_id: order.id})).order); say('Order cancelled. Nothing was sent.'); }
  catch (e) { say('Could not cancel: ' + (e.code || 'error')); }
});
