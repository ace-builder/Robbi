/** App-aware result stages. Only render returned facts; never run app tools here. */
const mounted = new WeakMap();
const MAX_ROWS = 80;
const interactions = new Map(), soundBuffers = new Map();
let soundContext=null, interactionSequence=0, sessionSoundPreference=false, playingCue=null;
function soundPreference() {
  try{const saved=globalThis.localStorage?.getItem('robbi:app-sounds');if(saved!==null&&saved!==undefined)return saved==='on';}catch{}
  return sessionSoundPreference;
}
function unlockSoundContext() {
  try{
    const Context=globalThis.AudioContext || globalThis.webkitAudioContext;
    if(!Context)return null;
    if(!soundContext || soundContext.state==='closed')soundContext=new Context();
    soundContext.resume()?.catch(()=>{});
    return soundContext;
  }catch{return null;}
}
function stopCue() {
  if(!playingCue)return;
  try{playingCue.stop();playingCue.disconnect();}catch{}
  playingCue=null;
}
async function loadCue(appId,context,signal) {
  if(soundBuffers.has(appId))return soundBuffers.get(appId);
  const response=await fetch(`/api/apps/${encodeURIComponent(appId)}/media/reveal.wav`,{signal,credentials:'same-origin'});
  if(!response.ok || Number(response.headers.get('content-length'))>2_000_000)throw Error('App sound unavailable');
  const bytes=await response.arrayBuffer();
  if(bytes.byteLength>2_000_000 || signal.aborted)throw Error('App sound unavailable');
  const buffer=await context.decodeAudioData(bytes);
  if(signal.aborted || !Number.isFinite(buffer.duration) || buffer.duration>3)throw Error('App sound unavailable');
  soundBuffers.set(appId,buffer);
  if(soundBuffers.size>8)soundBuffers.delete(soundBuffers.keys().next().value);
  return buffer;
}
/** Call synchronously inside the actual Run gesture, before awaiting submission. */
export function beginExperienceInteraction(appId) {
  stopCue();
  for(const [token,state] of interactions)if(state.appId===appId)cancelExperienceInteraction(token);
  const token=`rx-${Date.now()}-${++interactionSequence}`;
  const enabled=soundPreference(), controller=new AbortController(), context=enabled?unlockSoundContext():null;
  const state={appId,enabled,context,controller,createdAt:Date.now(),consumed:false,cancelled:false,ready:Promise.resolve(null)};
  if(context){
    const timeout=setTimeout(()=>controller.abort(),4000);
    state.ready=loadCue(appId,context,controller.signal).catch(()=>null).finally(()=>clearTimeout(timeout));
  }
  interactions.set(token,state);
  if(interactions.size>16)cancelExperienceInteraction(interactions.keys().next().value);
  return token;
}
export function cancelExperienceInteraction(token) {
  const state=interactions.get(token);if(!state)return;
  state.cancelled=true;state.controller.abort();
  if(state.source){try{state.source.stop();state.source.disconnect();}catch{}if(playingCue===state.source)playingCue=null;}
  interactions.delete(token);
}
const APPS = {
  'bookmarks': ['Saved for later', 'Your corner of the web', 'links', '#477db2'],
  'calculator': ['The answer is', 'A little clarity', 'calculation', '#597bcd'],
  'checklists': ['One thing at a time', 'Small steps, real progress', 'checklist', '#398c71'],
  'date-calculator': ['Make a little time', 'Days, counted carefully', 'calendar', '#ca6e4d'],
  'decision-helper': ['Let chance decide', 'A little nudge from the universe', 'chance', '#be8242'],
  'decision-notebook': ['Room to think', 'Your reasons, side by side', 'comparison', '#8672b5'],
  'expense-tracker': ['Follow the little things', 'Every expense has a place', 'ledger', '#4e9275'],
  'expressions': ['A little personality', 'Robbi has something to show you', 'expression', '#c276a6'],
  'fridge-rescue': ['Good things in the fridge', 'A little less waste, a little more dinner', 'kitchen', '#6c9346'],
  'gmail': ['Your post, thoughtfully sorted', 'A calmer inbox', 'mail', '#bd6659'],
  'google-calendar': ['Make room for your day', 'A little space for what matters', 'agenda', '#5086bc'],
  'google-maps': ['Somewhere to go', 'Your next little adventure', 'route', '#398e7c'],
  'habit-tracker': ['Keep a good thing going', 'Small rituals add up', 'habit', '#b17a3b'],
  'higher-lower': ['Somewhere in between', 'Follow the next clue', 'number-game', '#548cac'],
  'home-care': ['A little care goes a long way', 'A happy place to come home to', 'home', '#be8256'],
  'image-studio': ['An idea, made visible', 'From imagination to Robbi', 'image', '#a575ad'],
  'kitchen-timers': ['A little time on your side', 'Good things take a moment', 'timer', '#c77d43'],
  'meeting-prep': ['Arrive with a clear head', 'The useful things, gathered', 'brief', '#5d88a1'],
  'memory-colours': ['A colourful little challenge', 'Watch Robbi. Remember. Repeat.', 'memory', '#9a70b1'],
  'packing-list': ['A place for everything', 'Good to go, one item at a time', 'packing', '#588f8d'],
  'password-generator': ['A little more peace of mind', 'A private moment for new passwords', 'password', '#637ca0'],
  'percentage-helper': ['The bigger picture', 'Percentages, made clear', 'percentage', '#bc7758'],
  'quick-notes': ['Catch a little thought', 'A safe landing for your ideas', 'notes', '#b58a40'],
  'random-number': ['A little surprise', 'Fresh from the number machine', 'numbers', '#677cc2'],
  'ready-to-leave': ['Out the door, with a plan', 'A calmer way to get there', 'departure', '#4e9282'],
  'recipe-scaler': ['Just the right amount', 'Good food, your proportions', 'recipe', '#b68146'],
  'rhythm-relay': ['Find your rhythm', 'A little beat goes a long way', 'rhythm', '#b36d9d'],
  'robot-charades': ['What am I feeling?', 'A little theatre with Robbi', 'charades', '#b57254'],
  'robot-status': ['A little look inside', 'How Robbi is doing', 'status', '#568fa1'],
  'sensor-check': ['Hello, surroundings', 'What Robbi can sense right now', 'sensors', '#588f82'],
  'teach-me': ['Learn a little today', 'Read, recall, practise', 'learning', '#6c83b4'],
  'text-toolbox': ['Give your words a hand', 'A little polish for your text', 'text', '#a87958'],
  'tic-tac-toe': ['Your move, friend', 'Three in a row. A little friendly rivalry.', 'board', '#598c84'],
  'tiny-detective': ['A little mystery awaits', 'Follow the clues, trust your curiosity', 'detective', '#a38550'],
  'tip-splitter': ['A fair little share', 'Good company. Easy arithmetic.', 'receipt', '#bc775d'],
  'unit-converter': ['Another way to measure', 'The same thing, in your units', 'conversion', '#5b8eb1'],
  'weather': ['A little look outside', 'A forecast for your plans', 'weather', '#578db2'],
  'web-search': ['Follow your curiosity', 'A few useful places to begin', 'search', '#788c62'],
  'where-did-i-put-it': ['Everything has a place', 'A little help remembering yours', 'locations', '#b38351'],
  'world-clock': ['The world, in a moment', 'Different places. The same instant.', 'clocks', '#727bb3'],
};

export const escapeHTML = value => String(value ?? '').slice(0, 12000).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e = escapeHTML;
const arr = value => Array.isArray(value) ? value.slice(0, MAX_ROWS) : [];
const obj = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const present = value => value !== undefined && value !== null && value !== '';
const label = value => String(value ?? '').replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
const number = value => typeof value === 'number' && Number.isFinite(value) ? new Intl.NumberFormat(undefined, {maximumFractionDigits:8}).format(value) : String(value ?? '').slice(0, 500);
const pill = (text, cls='') => `<span class="rx-pill ${cls}">${e(text)}</span>`;
const chips = values => `<div class="rx-chips">${arr(values).map(v => pill(typeof v === 'object' ? v.name || v.title || v.text || '' : v)).join('')}</div>`;
const small = text => present(text) ? `<p class="rx-detail">${e(text)}</p>` : '';
const block = (heading, body, cls='') => `<section class="rx-block ${cls}">${heading ? `<h4>${e(heading)}</h4>` : ''}${body}</section>`;
const metric = (value, caption, unit='') => `<div class="rx-metric"><strong>${e(number(value))}${unit ? `<small>${e(unit)}</small>` : ''}</strong><span>${e(caption)}</span></div>`;
const metrics = items => `<div class="rx-metrics">${items.filter(item => present(item[0])).map(item => metric(...item)).join('')}</div>`;
const hero = (value, caption='', unit='') => `<div class="rx-hero-value"><strong>${e(number(value))}</strong>${unit ? `<span class="rx-unit">${e(unit)}</span>` : ''}${caption ? `<span class="rx-caption">${e(caption)}</span>` : ''}</div>`;
const rows = (values, render, cls='') => `<div class="rx-rows ${cls}">${arr(values).map(render).join('')}</div>`;
const row = (title, detail='', aside='', cls='') => `<div class="rx-row ${cls}"><div><strong>${e(title)}</strong>${small(detail)}</div>${aside ? `<span class="rx-row-aside">${e(aside)}</span>` : ''}</div>`;
const empty = text => `<div class="rx-empty"><span aria-hidden="true">✦</span><p>${e(text)}</p></div>`;
const progress = (done, total, text) => `<div class="rx-progress"><div role="progressbar" aria-label="${e(text)}" aria-valuemin="0" aria-valuemax="${e(Math.max(1,total))}" aria-valuenow="${e(Math.max(0,Math.min(done,total)))}"><span style="width:${total ? Math.max(0,Math.min(100,done/total*100)) : 0}%"></span></div><span>${e(text)}</span></div>`;
const dateText = value => {
  if (!present(value)) return '';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isFinite(date.valueOf()) ? date.toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}) : String(value).slice(0,150);
};
const clockText = value => {
  const match = String(value ?? '').match(/T(\d{2}:\d{2})/);
  return match ? match[1] : String(value ?? '').slice(0,80);
};
const safeURL = value => {
  if (typeof value !== 'string' || value.length > 8192) return '';
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''; } catch { return ''; }
};
const link = (url, title) => { const safe = safeURL(url); return safe ? `<a class="rx-link" href="${e(safe)}" target="_blank" rel="noopener noreferrer">${e(title)} <span aria-hidden="true">↗</span></a>` : `<span>${e(title)}</span>`; };
const description = result => result.text ? `<p class="rx-result-summary">${e(result.text)}</p>` : '';
const appAction = (text, action, answer='', tool='play') => `<button type="button" class="rx-button" data-rx-action="${e(action)}" data-rx-answer="${e(answer)}" data-rx-tool="${e(tool)}">${e(text)}</button>`;

function dice(value) {
  if (!Number.isInteger(value) || value < 1 || value > 6) return `<span class="rx-poly-die"><b>${e(value)}</b></span>`;
  const positions = {1:[5],2:[1,9],3:[1,5,9],4:[1,3,7,9],5:[1,3,5,7,9],6:[1,3,4,6,7,9]}[value];
  return `<span class="rx-die" role="img" aria-label="Die showing ${value}">${Array.from({length:9},(_,i)=>`<i class="${positions.includes(i+1)?'lit':''}"></i>`).join('')}</span>`;
}
function decision(d, result, options) {
  const values = arr(d.values);
  const coin = values.length === 1 && /^(heads?|tails?)$/i.test(String(values[0]));
  if (coin) {
    const heads = /^heads?$/i.test(values[0]);
    return `<div class="rx-coin-scene"><div class="rx-coin ${heads?'heads':'tails'}" role="img" aria-label="${e(values[0])}"><span class="rx-coin-face rx-coin-front"><i aria-hidden="true">✦</i><b>HEADS</b><img src="/api/apps/decision-helper/files/images/heads.png" alt="" width="146" height="146"></span><span class="rx-coin-face rx-coin-back"><i aria-hidden="true">☾</i><b>TAILS</b><img src="/api/apps/decision-helper/files/images/tails.png" alt="" width="146" height="146"></span></div><div class="rx-coin-shadow"></div></div>${hero(values[0], 'Chance has spoken')}`;
  }
  if (d.action === 'dice' || options.arguments?.action === 'dice' || (values.length && values.every(v=>typeof v === 'number'))) {
    return `<div class="rx-dice-tray">${values.map(v=>dice(v)).join('')}</div>${hero(values.reduce((a,b)=>a+Number(b),0),'Total')}`;
  }
  return values.length === 1 ? `<div class="rx-choice-orbit" aria-hidden="true">✦</div>${hero(values[0], 'Your pick')}` : rows(values,(v,i)=>row(v,'',String(i+1)),'rx-choice-order');
}
function checklist(d, packing=false) {
  const items = arr(d.items);
  if (!items.length) return arr(d.names || d.trips).length ? chips(d.names || d.trips) : empty(packing ? 'Your next adventure can start with one item.' : 'A fresh list. A little room to begin.');
  const total = packing ? items.reduce((s,v)=>s+(Number(v.quantity)||0),0) : items.length;
  const done = packing ? items.reduce((s,v)=>s+(Number(v.packed)||0),0) : items.filter(v=>v.done).length;
  return `<h3 class="rx-section-title">${e(d.name || d.trip || '')}</h3>${small(d.departure ? 'Departure · '+d.departure : '')}${progress(done,total,`${done} of ${total} ${packing?'packed':'complete'}`)}${rows(items,(v,i)=>`<div class="rx-check-row ${packing?v.remaining===0?'is-done':'':v.done?'is-done':''}"><span class="rx-check-mark" aria-hidden="true">${(packing?v.remaining===0:v.done)?'✓':e(v.number || i+1)}</span><div><strong>${e(v.text || v.name)}</strong>${small(packing?v.category:'')}</div>${packing?pill(`${v.packed} / ${v.quantity}`):''}</div>`)}`;
}
function calculator(d, result, appId, options) {
  const percent = present(d.percent_change);
  const value = percent ? d.percent_change : d.value;
  return `<div class="rx-calculation">${small(options.arguments?.expression || (appId==='unit-converter' && options.arguments ? `${options.arguments.value ?? ''} ${options.arguments.from_unit || options.arguments.from || ''}` : ''))}${hero(value,percent?'Percentage change':d.category || 'Result',percent?'%':d.unit || '')}${present(d.adjustment)||present(d.difference)?metrics([[d.adjustment,'Adjustment'],[d.difference,'Difference']]):''}</div>${small(d.convention)}`;
}
function numbers(d) {
  const values = arr(d.numbers);
  return `<div class="rx-number-machine">${values.map((v,i)=>`<span class="rx-number-ball" style="--rx-delay:${Math.min(i*45,600)}ms">${e(number(v))}</span>`).join('')}</div>${chips([`${d.minimum} → ${d.maximum}`,d.unique?'Distinct numbers':'Repeats allowed'])}${small(d.source)}`;
}
function receipt(d) {
  return `<div class="rx-receipt"><div class="rx-receipt-top">THE GOOD COMPANY RECEIPT</div>${row('Bill','',`${d.bill} ${d.currency}`)}${row('Tip','',`${d.tip} ${d.currency}`)}<div class="rx-receipt-total">${hero(d.total,'Total',d.currency)}</div>${rows(d.shares,(share,i)=>row(`Person ${i+1}`,'',`${share} ${d.currency}`))}${small(d.rounding)}</div>`;
}
function calendar(d) {
  return present(d.date) ? `<div class="rx-date-leaf"><span>${e(d.weekday)}</span><strong>${e(String(d.date).slice(-2))}</strong><b>${e(String(d.date).slice(0,7))}</b></div>${small(`${d.days} calendar days shifted`)}` : `<div class="rx-date-range">${pill(d.start)}<span aria-hidden="true">⟶</span>${pill(d.end)}</div>${metrics([[d.calendar_days,'Calendar days'],[d.business_days,'Weekdays']])}${small(d.convention)}`;
}
function clocks(d) {
  return rows(d.times,v=>{
    const time = String(v.datetime).match(/T(\d{2}):(\d{2})/);
    const hour = time ? Number(time[1]) : 0, minute = time ? Number(time[2]) : 0;
    return `<div class="rx-clock-card"><div class="rx-clock-face" aria-hidden="true"><i class="rx-hour-hand" style="transform:rotate(${hour*30+minute/2}deg)"></i><i class="rx-minute-hand" style="transform:rotate(${minute*6}deg)"></i><b></b></div><div><span class="rx-clock-digital">${e(clockText(v.datetime))}</span><strong>${e(v.timezone)}</strong>${small(`${String(v.datetime).split('T')[0]} · ${v.abbreviation || ''} · ${String(v.datetime).slice(-6)}`)}</div></div>`;
  });
}
function weather(d) {
  const condition=String(d.condition || ''), rainy=/rain|drizzle|shower|thunder/i.test(condition), snow=/snow|sleet/i.test(condition), cloud=/cloud|overcast|fog/i.test(condition);
  const symbol=snow?'❄':rainy?'☂':cloud?'☁':'☀';
  return `<div class="rx-weather-sky ${rainy?'rain':snow?'snow':cloud?'cloud':'sun'}"><span class="rx-weather-symbol" aria-hidden="true">${symbol}</span><div><span class="rx-eyebrow">${e(d.location?.name || '')} · ${e(d.date)}</span><h3>${e(condition)}</h3>${hero(present(d.temperature_max)?d.temperature_max:d.temperature_min,'Forecast',d.units?.temperature || '°C')}</div></div>${metrics([[d.temperature_min,'Low',d.units?.temperature || '°C'],[d.temperature_max,'High',d.units?.temperature || '°C'],[d.precipitation,'Rain',d.units?.precipitation || 'mm'],[d.precipitation_probability,'Rain chance','%'],[d.wind_speed_max,'Wind',d.units?.wind_speed || 'km/h']])}${d.stale?`<p class="rx-warning">Cached forecast is stale. Check current conditions.</p>`:d.cached?small('Cached forecast'):''}${small(d.range_note || d.condition_basis)}${d.partial_day?small('Partial day forecast'):''}${small(d.retrieved_at ? 'Retrieved '+dateText(d.retrieved_at) : '')}${arr(d.attribution).length?block('Weather sources',rows(d.attribution,v=>`<div>${link(v.url,v.name)}${small(v.license)}${small(v.changes)}</div>`)):''}`;
}
function board(d,result,appId,options={}) {
  const interactive=typeof options.onAction==='function';
  if (!Array.isArray(d.board) || d.board.length !== 9) return empty(d.active===false?'This game has finished. Start a new round.':'Start a game to see the board.')+(interactive?appAction('Start a game','start'):'');
  const winning = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]].find(line=>d.board[line[0]] && line.every(i=>d.board[i]===d.board[line[0]])) || [];
  return `<div class="rx-board-legend">${pill('You · X')}${pill('Robbi · O')}</div><div class="rx-board" role="${interactive?'group':'img'}" aria-label="Tic tac toe board: ${e(d.board.map((v,i)=>`${i+1} ${v||'empty'}`).join(', '))}">${d.board.map((v,i)=>!v&&d.active&&interactive?`<button type="button" class="rx-square rx-square-action" aria-label="Play square ${i+1}" data-rx-action="move" data-rx-answer="${i+1}" data-rx-tool="play"><small>${i+1}</small></button>`:`<span class="rx-square ${v==='X'?'is-x':v==='O'?'is-o':''} ${winning.includes(i)?'is-winning':''}">${v?`<b>${e(v)}</b>`:`<small>${i+1}</small>`}</span>`).join('')}</div>${d.winner?hero(d.winner==='draw'?'A good draw':d.winner==='X'?'You win!':'Robbi wins!','Shall we play again?'):small(interactive?'Choose an empty square or tell Robbi your move.':'Choose an empty square in the tool controls or tell Robbi your move.')}${present(d.moves)?chips([`${d.moves} moves`]):''}${interactive&&d.active===false?appAction('Play again','start'):''}`;
}
function higherLower(d,result,appId,options={}) {
  const low=Number(d.low), high=Number(d.high);
  return present(d.low)&&present(d.high) ? `<div class="rx-range-game"><span class="rx-range-end">${e(d.low)}</span><div class="rx-range-thread"><i></i><i></i><i></i></div><span class="rx-range-end">${e(d.high)}</span></div>${hero(d.active===false && d.winner==='user'?'You found it!':`${high-low+1} possibilities`,d.active?'Your next guess?':'Round finished')}${chips([`${d.moves || 0} guesses`])}${typeof options.onAction==='function'?(d.active?`<form class="rx-guess-form" data-rx-guess><label>Your guess<input type="number" name="guess" min="${e(low)}" max="${e(high)}" step="1" inputmode="numeric" required placeholder="${e(low)}–${e(high)}"></label><button type="submit" class="rx-button">Make a guess</button></form>`:appAction('Play again','start')):''}` : empty('Start a round. Robbi will keep the number a secret.')+(typeof options.onAction==='function'?appAction('Start a game','start'):'');
}
function memory(d,result,appId,options={}) {
  const interactive=typeof options.onAction==='function';
  return `<div class="rx-memory-pads">${['red','blue','green','yellow'].map((colour,i)=>{const body=`<span aria-hidden="true">${['●','◆','▲','■'][i]}</span><b>${colour}</b>`;return interactive&&d.active?`<button type="button" class="rx-memory-pad ${colour}" data-rx-action="move" data-rx-answer="${colour}" data-rx-tool="play" aria-label="Answer ${colour}">${body}</button>`:`<div class="rx-memory-pad ${colour}">${body}</div>`;}).join('')}</div>${d.winner?hero(d.winner==='user'?'Perfect memory!':'Nice try!'):small(interactive?'Watch the sequence on Robbi’s screen, then tap the colours in order.':'Watch the sequence on Robbi’s screen, then repeat the colours using the tool or your voice.')}${present(d.index)?chips([`${d.index} colours recalled`]):''}${interactive?appAction(d.active?'Replay on Robbi':'Play again',d.active?'status':'start'):''}`;
}
function rhythm(d) {
  const intervals=arr(d.intervals_s).filter(v=>Number.isFinite(v)&&v>0).slice(0,16), times=[0];
  intervals.forEach(v=>times.push(times[times.length-1]+v));
  const total=times[times.length-1] || 1;
  return `${chips([`Level ${d.level || 1}`,d.mode || 'solo',`${d.attempts || 0} attempts`])}<div class="rx-beat-timeline" aria-label="Rhythm timeline">${times.map((time,i)=>`<span class="rx-beat" style="--rx-beat-position:${time/total*86+7}%;--rx-beat-delay:${time}s"><i>${e(arr(d.players)[i] || i+1)}</i><small>${time.toFixed(2)}s</small></span>`).join('')}</div>${intervals.length?`<button type="button" class="rx-button" data-rx-rhythm>Demonstrate rhythm</button>`:small('Start a round to get a rhythm.')}${metrics([[d.timing_score ?? d.last_timing_score,'Timing score'],[d.claps,'Claps']])}${small('The demonstration follows the returned beat intervals. Submit real taps in the app dashboard.')}`;
}
function detective(d) {
  return `${chips([present(d.case_number)?`Case ${d.case_number}`:'Casebook',`${d.score || 0} points`])}<div class="rx-case-file"><span class="rx-case-stamp">CASE FILE</span><h3>${e(d.title || 'Ready for a mystery?')}</h3>${small(d.question)}${chips(d.choices)}${arr(d.evidence).length?block('Evidence collected',rows(d.evidence,v=>row(label(v.id),v.text))):small('Inspect a clue with the app tool to gather evidence.')}${arr(d.hints).length?block('Hints',rows(d.hints,v=>row(v))):''}${present(d.answer)?block('Case closed',`<strong>${e(d.answer)}</strong>${small(d.explanation)}`):''}</div>`;
}
function charades(d) {
  return `${chips([d.category || 'Charades',`Round ${d.round || 0}`,`${d.score || 0} points`])}<div class="rx-charades-card"><span class="rx-curtain rx-curtain-left" aria-hidden="true"></span><span class="rx-theatre-star" aria-hidden="true">✦</span><p>${e(d.scene || 'Start a round and watch Robbi perform.')}</p><span class="rx-curtain rx-curtain-right" aria-hidden="true"></span></div>${arr(d.hints).length?block('A little hint',rows(d.hints,v=>row(v))):''}${present(d.answer)?hero(d.answer,'The answer'):small('Watch Robbi’s expression and make your guess.')}`;
}
function notes(d) {
  const values=Array.isArray(d.notes)?d.notes:present(d.text)?[d]:[];
  return values.length?`<div class="rx-note-wall">${arr(values).map((v,i)=>`<article class="rx-note" style="--rx-note-tilt:${i%2?1:-1}deg"><span class="rx-note-pin" aria-hidden="true"></span><p>${e(v.text)}</p><footer>${e(v.id)}${v.created_at?` · ${e(dateText(v.created_at))}`:''}</footer></article>`).join('')}</div>`:empty('A fresh page for your next thought.');
}
function habits(d) {
  const values=arr(d.habits || (d.habit?[d.habit]:[]));
  return values.length?rows(values,v=>`<div class="rx-habit-card"><div class="rx-streak-badge"><strong>${e(v.current_streak)}</strong><span>days</span></div><div><h3>${e(v.name)}</h3>${chips([v.completed_today?'Done today':'Today is still open',`Best: ${v.best_streak} days`,`${v.completed_days} days logged`])}<div class="rx-habit-dates" aria-label="Last logged dates">${arr(v.dates).slice(-14).map(date=>`<span title="${e(date)}">✓<small>${e(String(date).slice(5))}</small></span>`).join('')}</div></div></div>`):empty('Every habit begins with one small day.');
}
function expenses(d) {
  const entries=arr(d.entries || (d.expense?[d.expense]:[]));
  return `${metrics(Object.entries(obj(d.totals)).slice(0,12).map(([currency,value])=>[value,'Total',currency]))}${entries.length?rows(entries,v=>row(v.category,`${v.date || ''}${v.note?' · '+v.note:''}${v.id?' · ID '+v.id:''}`,`${v.amount} ${v.currency}`),'rx-ledger'):empty('No expenses in this selection.')}${small(d.note)}`;
}
function bookmarks(d) {
  const values=arr(d.bookmarks || (d.bookmark?[d.bookmark]:[]));
  return values.length?rows(values,v=>`<article class="rx-bookmark"><span class="rx-bookmark-ribbon" aria-hidden="true">↗</span><div>${link(v.url,v.title)}${small(v.url)}${chips(v.tags)}${small(v.id ? 'ID '+v.id : '')}</div></article>`):empty('Save a useful little corner of the web.');
}
function fridge(d) {
  if(Array.isArray(d.recipes)) return d.recipes.length?rows(d.recipes,v=>block(v.name,`${chips(v.matched)}${arr(v.missing).length?small('Still needed: '+v.missing.join(', ')):pill('All listed ingredients in stock')}${arr(v.use_soon).length?small('Use soon: '+v.use_soon.join(', ')):''}<ol>${arr(v.steps).map(step=>`<li>${e(step)}</li>`).join('')}</ol>`,'rx-recipe-card')):empty('No recipe ideas match your current stock.');
  const items=arr(d.items || (d.item?[d.item]:[]));
  return items.length?rows(items,v=>row(v.name,[present(v.quantity)?v.quantity:'',v.unit || '',v.expires?'Date label: '+v.expires:'',v.id?'ID '+v.id:''].filter(Boolean).join(' · '),present(v.bought)?v.bought?'Bought':'To buy':v.location || ''),'rx-pantry'):empty('Room for something fresh.');
}
function chores(d) {
  const values=arr(d.chores || (d.chore?[d.chore]:[]));
  return values.length?rows(values,v=>`<div class="rx-chore"><span class="rx-chore-icon" aria-hidden="true">⌂</span><div><strong>${e(v.name)}</strong>${small(`${v.area || v.room || ''}${v.id?' · ID '+v.id:''}`)}</div>${pill(v.next_due || '')}${present(v.days_until_due)?pill(v.days_until_due<0?`${Math.abs(v.days_until_due)} days overdue`:v.days_until_due===0?'Due today':`In ${v.days_until_due} days`,v.days_until_due<0?'rx-alert':''):''}</div>`):empty('A little breathing room at home.');
}
function locations(d) {
  const values=arr(d.items || (d.item?[d.item]:[]));
  return values.length?rows(values,v=>`<div class="rx-location-card"><span class="rx-location-pin" aria-hidden="true">⌖</span><div><h3>${e(v.name)}</h3><p>${e(v.location || v.place || '')}</p>${small(v.notes || v.note)}${/^\/api\/apps\/where-did-i-put-it\/artifacts\/[a-zA-Z0-9_-]{1,64}$/.test(v.photo_url || '')?`<img class="rx-record-photo" src="${e(v.photo_url)}" alt="${e(v.name)} · saved photo" loading="lazy">`:''}${chips([v.status,v.id?'ID '+v.id:''].filter(Boolean))}</div></div>`):empty('Remember a place now, find it again later.');
}
function comparison(d) {
  const c=obj(d.comparison || d.decision);
  if(Array.isArray(c.options) && c.options.some(v=>typeof v === 'object')) return `<h3>${e(c.title)}</h3>${rows(c.options,v=>`<div class="rx-comparison-option"><div><strong>${e(v.name)}</strong>${present(v.weighted_score)?pill(`${v.weighted_score} / 10`):pill('Ratings incomplete')}</div>${present(v.weighted_score)?progress(Number(v.weighted_score),10,'Weighted score'):''}${rows(v.reasons,r=>row(r.criterion,r.reason,`${r.score}/10 · weight ${r.weight}`))}</div>`)}${arr(c.missing).length?small(`${c.missing.length} ratings missing. Ranking is available after all ratings are complete.`):chips(c.ranking)}${small(c.notes)}${small(c.outcome)}`;
  const values=arr(d.decisions || (d.decision?[d.decision]:[]));
  return values.length?rows(values,v=>block(v.title,`${chips(v.options)}${small(v.id?'ID '+v.id:'')}${chips(v.criteria)}`)):empty('Give your next decision a little space to grow.');
}
function learning(d,result,appId,options={}) {
  if(d.lesson){const l=obj(d.lesson),interactive=typeof options.onAction==='function';return `<div class="rx-study-card">${pill(l.topic || 'Five minute lesson')}<h3>${e(l.title)}</h3>${small(l.lesson)}${block('Recall',`<p>${e(l.question)}</p>`)}${block('Practise',small(l.practice))}${l.revealed&&present(l.answer)?block('Compare your answer',small(l.answer)):small(interactive?'Try recalling the answer before you reveal it.':'Try recalling the answer, then use Reveal in the tool controls.')}${interactive?l.revealed?`<div class="rx-chips">${['again','hard','good','easy'].map(grade=>`<button type="button" class="rx-button" data-rx-action="grade" data-rx-grade="${grade}" data-rx-tool="study">${label(grade)}</button>`).join('')}</div>`:appAction('Reveal answer','reveal','','study'):''}</div>`;}
  if(Array.isArray(d.topics))return typeof options.onAction==='function'?`<div class="rx-chips">${arr(d.topics).map(topic=>`<button type="button" class="rx-button" data-rx-action="start" data-rx-topic="${e(topic)}" data-rx-tool="study">${e(topic)}</button>`).join('')}</div>`:chips(d.topics);
  if(present(d.studied))return metrics([[d.studied,'Cards studied'],[arr(d.due).length,'Reviews due'],[arr(d.difficult).length,'Difficult cards']]);
  if(Array.isArray(d.cards))return rows(d.cards,v=>block(v.title,`${pill(v.topic)}${small(v.question)}${small('Card ID: '+v.id)}`));
  const due=d.due || d.next_review;
  return present(due)?hero(clockText(due),'Next review · '+String(due).split('T')[0]):empty('A little curiosity is a good place to start.');
}
function textTools(d) {
  return present(d.words)?`${metrics([[d.words,'Words'],[d.characters,'Characters'],[d.characters_without_whitespace,'Without spaces'],[d.lines,'Lines'],[d.utf8_bytes,'UTF-8 bytes']])}${small(d.note)}`:present(d.text)?`<div class="rx-text-paper"><pre>${e(d.text)}</pre><button type="button" class="rx-button" data-rx-copy-text>Copy text</button></div>`:empty('Your words have room to breathe.');
}
function password(d) {
  return `<div class="rx-secret-box"><span aria-hidden="true" class="rx-secret-symbol">⌑</span><h3>Your passwords are ready</h3>${small('Hidden until you choose to reveal them. This result may still be retained in job or chat history.')}${rows(d.passwords,(v,i)=>`<div class="rx-secret-row"><span>Password ${i+1}</span><code data-rx-secret="${i}">••••••••••••</code><button class="rx-button" type="button" data-rx-reveal="${i}" aria-pressed="false">Reveal</button><button class="rx-button" type="button" data-rx-copy-secret="${i}">Copy</button></div>`)}${small(`${d.length} characters · ${d.source || ''}`)}${small(d.note)}</div>`;
}
function timer(d) {
  const values=Array.isArray(d.timers)?d.timers:present(d.due_at)?[d]:[];
  return values.length?rows(values,v=>`<div class="rx-timer-card"><div class="rx-timer-ring"><span data-rx-due="${e(v.due_at)}">${e(formatSeconds(present(v.remaining_s)?v.remaining_s:Math.max(0,Number(v.due_at)-Date.now()/1000)))}</span></div><div><h3>${e(String(v.message || v.label || 'Kitchen timer').replace(/^\[Kitchen\] /,''))}</h3>${small('Due '+dateText(v.due_at))}${small('Timer ID: '+v.id)}<span class="rx-timer-browser-note" hidden>Time reached · check Robbi for timer status</span></div></div>`):empty('No active timers in this result.');
}
function formatSeconds(value) {const seconds=Math.max(0,Math.ceil(Number(value)||0));return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;}
function agenda(d) {
  const values=arr(d.events || (d.event?[d.event]:[]));
  return values.length?`<div class="rx-agenda">${values.map(v=>`<article class="rx-agenda-event"><div class="rx-agenda-time"><strong>${e(clockText(v.start))}</strong><span>${e(String(v.start || '').split('T')[0])}</span></div><div><h3>${e(v.title)}</h3>${small(v.location)}${small(`${v.start || ''} → ${v.end || ''}`)}${v.url?link(v.url,'Open event'):''}${small(v.id?'ID '+v.id:'')}</div></article>`).join('')}</div>${small(d.timezone)}${d.has_more?small('More events exist. Narrow the date range to see them.'):''}`:empty('A little space in the calendar.');
}
function gmail(d) {
  if(Array.isArray(d.messages))return d.messages.length?rows(d.messages,v=>`<div class="rx-mail"><span class="rx-mail-icon" aria-hidden="true">✉</span><div><strong>${e(v.subject || '(no subject)')}</strong>${small(v.from)}${small(v.snippet)}${v.url?link(v.url,'Open in Gmail'):small('Message ID: '+v.id)}</div></div>`):empty('No matching mail. A little more quiet.');
  if(present(d.body))return `<article class="rx-letter"><span class="rx-letter-label">MESSAGE PREVIEW</span><h3>${e(d.subject || '(no subject)')}</h3>${small('From: '+(d.from || 'Unknown sender'))}<pre>${e(d.body)}</pre>${d.truncated||d.body_from_snippet?small('Shortened preview. Open Gmail for the full message and attachments.'):''}</article>`;
  if(present(d.to))return `<div class="rx-letter"><span class="rx-letter-label">DRAFT SAVED</span><h3>${e(d.subject)}</h3>${small('To: '+d.to)}${small('Review and send this draft in Gmail.')}${link(d.url,'Review draft')}</div>`;
  return '';
}
function maps(d) {
  return `<div class="rx-route-art" aria-hidden="true"><span>A</span><i></i><span>B</span></div>${d.link_only?`<div class="rx-map-link">${pill('Map link ready')}${small('Open Google Maps to retrieve places or calculate the route.')}${link(d.url,'Open Google Maps')}</div>`:`${metrics([[d.distance_meters ?? d.distance_m,'Distance','m'],[d.duration_seconds ?? d.duration_s,'Estimated journey','s']])}${rows(d.places,v=>block(v.address,`${small(`${v.latitude}, ${v.longitude}`)}${v.url?link(v.url,'Open place'):''}`))}${small(d.formatted_address || d.address)}${present(d.latitude)&&present(d.longitude)?small(`${d.latitude}, ${d.longitude}`):''}${d.url?link(d.url,'Open Google Maps'):''}${d.traffic_aware===false?small('Estimate does not include live traffic.'):''}`}${rows(d.warnings,v=>small(v))}`;
}
function search(d) {
  return arr(d.results).length?rows(d.results,(v,i)=>`<article class="rx-search-result"><span class="rx-search-number">${i+1}</span><div>${link(v.url,v.title)}${small(v.snippet)}${small(v.url)}</div></article>`):empty('No results in this search. Try a different phrase.');
}
function departure(d) {
  if(Array.isArray(d.events))return agenda(d);
  const plan=obj(d.plan || arr(d.plans).at(-1));
  if(!Object.keys(plan).length)return `${block('Preferences',rows(Object.entries(obj(d.preferences)),([key,value])=>row(label(key),Array.isArray(value)?value.join(', '):String(value ?? ''))))}`;
  return `<div class="rx-departure-ticket"><span class="rx-ticket-label">YOUR DEPARTURE PLAN</span>${plan.departure?hero(clockText(plan.departure),'Leave at · '+String(plan.departure).split('T')[0]):hero('Time needed','Add a travel estimate to calculate departure')}${row(plan.event?.title || plan.destination || 'Journey',plan.destination)}${metrics([[present(plan.travel_seconds)?Math.round(plan.travel_seconds/60):null,'Travel','min'],[plan.buffer_minutes,'Buffer','min']])}${arr(plan.carry_items).length?block('Take along',chips(plan.carry_items)):''}${pill(plan.reminder?.status==='none'?'No reminder scheduled':plan.reminder?.status || (d.reminder_scheduled?'Reminder scheduled':'No reminder confirmed'))}</div>${rows(d.warnings,v=>`<p class="rx-warning">${e(v)}</p>`)}`;
}
function meeting(d) {
  if(d.selection_required)return agenda(d);
  const record=obj(d.record);
  if(d.event)return `${agenda({events:[d.event]})}${block('Open actions',arr(d.open_actions).length?rows(d.open_actions,v=>row(v)):small('No saved open actions for this topic.'))}${block('Related mail',arr(d.threads).length?rows(d.threads,v=>`${link(v.url,'Open conversation')}${rows(v.messages,m=>row(m.subject,m.snippet,m.from))}`):small('No mail summaries included.'))}${rows(d.warnings,v=>`<p class="rx-warning">${e(v)}</p>`)}`;
  const records=arr(d.records || (Object.keys(record).length?[record]:[]));
  return records.length?rows(records,v=>block(v.topic,`${small(v.notes)}${rows(v.actions,a=>row(typeof a==='object'?a.text || a.title || 'Action':a,'',a.done?'Complete':'Open'))}${small(v.id?'Record ID: '+v.id:'')}`)):empty('Gather a meeting’s useful details in one place.');
}
function sensors(d) {
  return `<div class="rx-sensor-grid">${Object.entries(obj(d.tof)).map(([name,v])=>`<div class="rx-sensor"><span class="rx-sensor-rings" aria-hidden="true">◉</span><h3>${e(name.toUpperCase())}</h3>${present(v.distance_cm)?hero(v.distance_cm,'Distance','cm'):hero('—','No valid range')}${pill(v.enabled===false?'Disabled':v.fresh?'Fresh':v.available?'Stale':'Unavailable',v.fresh?'':'rx-alert')}</div>`).join('')}</div>${block('Orientation',metrics(Object.entries(obj(d.imu?.orientation_deg)).filter(([key])=>['roll','pitch','yaw'].includes(key)).map(([key,value])=>[value,label(key),'°']))+chips([d.imu?.fresh?'IMU fresh':'IMU not fresh',d.imu?.calibration || ''].filter(Boolean)))}${small('Relative yaw is not a compass bearing.')}`;
}
function robotStatus(d) {
  return `<div class="rx-status-orb ${d.state==='ready'?'ready':''}"><span aria-hidden="true">✦</span></div>${hero(label(d.state || 'Unknown'),'Robot health')}${metrics([[present(d.uptime_s)?Math.floor(d.uptime_s/60):null,'Uptime','min']])}${arr(d.issues).length?block('Needs attention',rows(d.issues,v=>row(typeof v==='string'?v:v.message || v.name || v.state || 'Issue'))):small('No issues reported in this snapshot.')}${arr(d.disabled).length?block('Disabled',chips(d.disabled)):''}`;
}
function imageStudio(d) {
  return `<div class="rx-image-receipt"><span class="rx-image-spark" aria-hidden="true">✦</span><div><h3>${d.displayed?'On Robbi’s screen':'Your image result'}</h3>${chips([d.provider,d.model].filter(Boolean))}${small(present(d.displayed)?d.displayed?'Robot display confirmed.':'Robot display was not confirmed.':'')}${small(d.reason)}${small('View the actual returned image in the result artifacts below.')}</div></div>`;
}
function expressions(d) {
  return `<div class="rx-expression-face" aria-hidden="true"><span>•</span><span>•</span><i></i></div>${chips([d.displayed?'Screen confirmed':'Screen not confirmed',present(d.audio_state)?'Audio: '+d.audio_state:'',present(d.loops_completed)?`${d.loops_completed} loops completed`:''].filter(Boolean))}${metrics([[d.frames_displayed,'Frames displayed']])}${small(d.reason)}`;
}
function recipe(d) {
  return `${hero(d.factor,'Scale factor','×')}${rows(d.ingredients,v=>`<div class="rx-ingredient"><span class="rx-ingredient-dot" aria-hidden="true"></span><strong>${e(v.name)}</strong><span>${e(v.amount)} <small>${e(v.unit)}</small></span></div>`)}${small(d.note)}`;
}

const renderers = {
  'bookmarks':bookmarks,'calculator':calculator,'checklists':d=>checklist(d),
  'date-calculator':calendar,'decision-helper':decision,'decision-notebook':comparison,
  'expense-tracker':expenses,'expressions':expressions,'fridge-rescue':fridge,'gmail':gmail,
  'google-calendar':agenda,'google-maps':maps,'habit-tracker':habits,'higher-lower':higherLower,
  'home-care':chores,'image-studio':imageStudio,'kitchen-timers':timer,'meeting-prep':meeting,
  'memory-colours':memory,'packing-list':d=>checklist(d,true),'password-generator':password,
  'percentage-helper':calculator,'quick-notes':notes,'random-number':numbers,
  'ready-to-leave':departure,'recipe-scaler':recipe,'rhythm-relay':rhythm,'robot-charades':charades,
  'robot-status':robotStatus,'sensor-check':sensors,'teach-me':learning,'text-toolbox':textTools,
  'tic-tac-toe':board,'tiny-detective':detective,'tip-splitter':receipt,'unit-converter':calculator,
  'weather':weather,'web-search':search,'where-did-i-put-it':locations,'world-clock':clocks,
};

/** Pure markup entry point, also used by regression tests. */
export function buildExperienceMarkup(result, appId, options={}) {
  if(!result || result.permission_required || result.error || !renderers[appId] || (result.ok===false && !(appId==='sensor-check' && (result.data?.tof || result.data?.imu)))) return '';
  const [heading,subtitle,kind,accent]=APPS[appId], d=obj(result.data);
  const content=appId==='decision-helper'?decision(d,result,options):renderers[appId](d,result,appId,options);
  if(!content && !result.text)return '';
  return `<article class="rx-experience rx-kind-${kind}" data-rx-app="${e(appId)}" style="--rx-accent:${accent}"><header class="rx-header"><img class="rx-app-icon" src="/api/apps/${encodeURIComponent(appId)}/images/icon" alt="" width="52" height="52"><div><span class="rx-eyebrow">${e(options.appName || label(appId.replaceAll('-',' ')))}</span><h3>${e(heading)}</h3><p>${e(subtitle)}</p></div><span class="rx-header-star" aria-hidden="true">✦</span></header><div class="rx-stage">${content}</div>${appId==='password-generator'?'':description(result)}<footer class="rx-footer"><span class="rx-result-label"><i></i>${result.ok===false?'Sensor report':'Result received'}</span><div class="rx-sound-options"><label class="rx-sound-preference"><input type="checkbox" data-rx-sound-enabled> App sounds</label><button type="button" class="rx-sound-control" data-rx-sound aria-pressed="false" disabled>♪ Play sound</button></div></footer><span class="rx-feedback" role="status" aria-live="polite"></span></article>`;
}

export function getExperienceCoverage() {return Object.entries(APPS).map(([id,[heading,,kind]])=>({id,heading,kind,renderer:renderers[id].name || kind}));}
export function teardownExperience(target) {const prior=mounted.get(target);if(prior){prior.destroy();mounted.delete(target);}}

/**
 * Mount once per returned result. Pass a stable target across polling; jobId lets
 * two identical coin outcomes from different requests animate independently.
 * Automatic audio needs a token minted by an explicit Run gesture after opt-in.
 */
export function renderExperience(target,result,appId,options={}) {
  if(!target)return {handled:false,destroy(){}};
  let signature;
  try{signature=JSON.stringify([appId,options.jobId ?? null,result,options.arguments ?? null,typeof options.onAction==='function']);}catch{return {handled:false,destroy(){}};}
  const prior=mounted.get(target);
  if(prior?.signature===signature && target.firstElementChild)return {handled:true,destroy:()=>teardownExperience(target)};
  const markup=buildExperienceMarkup(result,appId,options);
  if(!markup){teardownExperience(target);cancelExperienceInteraction(options.interactionToken);return {handled:false,destroy(){}};}
  teardownExperience(target);
  target.innerHTML=markup;
  const abort=new AbortController(), timeouts=new Set(), d=obj(result.data);
  let audio=null, tick=null, autoSource=null;
  const reduced=globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const soundPreferenceControl=target.querySelector('[data-rx-sound-enabled]'),soundButton=target.querySelector('[data-rx-sound]');
  let soundEnabled=options.soundEnabled===undefined?soundPreference():options.soundEnabled===true;
  if(soundPreferenceControl)soundPreferenceControl.checked=soundEnabled;
  if(soundButton)soundButton.disabled=!soundEnabled;
  const feedback=text=>{const node=target.querySelector('.rx-feedback');if(node)node.textContent=text;};
  const later=(fn,delay)=>{const handle=setTimeout(()=>{timeouts.delete(handle);if(!abort.signal.aborted)fn();},delay);timeouts.add(handle);};
  const on=(node,type,fn)=>node?.addEventListener(type,fn,{signal:abort.signal});
  const stopLocalAudio=()=>{
    cancelExperienceInteraction(options.interactionToken);
    if(autoSource){try{autoSource.stop();autoSource.disconnect();}catch{}autoSource=null;}
    if(audio){audio.pause();audio=null;}
    if(soundButton){soundButton.setAttribute('aria-pressed','false');soundButton.textContent='♪ Play sound';}
  };
  on(globalThis.document,'robbi:stop',stopLocalAudio);
  on(globalThis.document,'visibilitychange',()=>{if(globalThis.document.hidden)stopLocalAudio();});
  let actionPending=false;
  const performAction=async payload=>{
    if(actionPending || typeof options.onAction!=='function')return;
    actionPending=true;
    const controls=target.querySelectorAll('[data-rx-action], [data-rx-guess] input, [data-rx-guess] button');
    controls.forEach(node=>node.disabled=true);feedback('Sending your request…');
    try{await options.onAction(payload);if(!abort.signal.aborted)feedback('Request sent. Waiting for Robbi’s result.');}
    catch(error){if(!abort.signal.aborted)feedback(error?.message || 'The request could not be sent. Try again.');}
    finally{actionPending=false;if(!abort.signal.aborted)controls.forEach(node=>node.disabled=false);}
  };
  on(target,'submit',event=>{
    const form=event.target.closest('[data-rx-guess]');if(!form)return;
    event.preventDefault();const value=Number(form.elements.guess.value);
    if(!Number.isInteger(value)||value<Number(d.low)||value>Number(d.high)){feedback(`Choose a whole number from ${d.low} to ${d.high}.`);return;}
    performAction({toolName:'play',arguments:{action:'move',answer:String(value)}});
  });
  on(soundPreferenceControl,'change',()=>{
    soundEnabled=soundPreferenceControl.checked;
    sessionSoundPreference=soundEnabled;
    if(soundButton)soundButton.disabled=!soundEnabled;
    if(!soundEnabled){stopCue();if(autoSource){try{autoSource.stop();autoSource.disconnect();}catch{}autoSource=null;}if(audio){audio.pause();audio=null;if(soundButton){soundButton.setAttribute('aria-pressed','false');soundButton.textContent='♪ Play sound';}}}
    if(soundEnabled)unlockSoundContext();
    try{globalThis.localStorage?.setItem('robbi:app-sounds',soundEnabled?'on':'off');}catch{}
    options.onSoundPreferenceChange?.(soundEnabled);
  });
  target.querySelectorAll('.rx-app-icon,.rx-record-photo,.rx-coin-face img').forEach(node=>on(node,'error',()=>{node.hidden=true;}));
  on(target,'click',async event=>{
    const button=event.target.closest('button');if(!button || !target.contains(button))return;
    if(button.hasAttribute('data-rx-action')){
      const args={action:button.dataset.rxAction};
      if(present(button.dataset.rxAnswer))args.answer=button.dataset.rxAnswer;
      if(present(button.dataset.rxGrade))args.grade=button.dataset.rxGrade;
      if(present(button.dataset.rxTopic))args.topic=button.dataset.rxTopic;
      performAction({toolName:button.dataset.rxTool || 'play',arguments:args});
    }
    if(button.hasAttribute('data-rx-reveal')) {
      const index=Number(button.dataset.rxReveal), node=target.querySelector(`[data-rx-secret="${index}"]`), revealing=button.getAttribute('aria-pressed')!=='true';
      if(node){node.textContent=revealing?String(arr(d.passwords)[index] ?? ''):'••••••••••••';button.textContent=revealing?'Hide':'Reveal';button.setAttribute('aria-pressed',String(revealing));}
    }
    if(button.hasAttribute('data-rx-copy-secret') || button.hasAttribute('data-rx-copy-text')) {
      const secret=button.hasAttribute('data-rx-copy-secret'), value=secret?arr(d.passwords)[Number(button.dataset.rxCopySecret)]:d.text;
      try{if(!globalThis.navigator?.clipboard?.writeText)throw Error();await navigator.clipboard.writeText(String(value ?? ''));feedback(secret?'Password copied.':'Text copied.');}catch{feedback('Clipboard unavailable. Reveal and select the text to copy it.');}
    }
    if(button.hasAttribute('data-rx-rhythm')) {
      const beats=target.querySelectorAll('.rx-beat');
      let offset=0;button.disabled=true;beats.forEach(node=>node.classList.remove('is-playing'));
      beats.forEach((node,index)=>{if(index)offset+=Number(arr(d.intervals_s)[index-1])||0;later(()=>{node.classList.add('is-playing');later(()=>node.classList.remove('is-playing'),200);},offset*1000);});
      later(()=>{button.disabled=false;feedback('Demonstration complete. Your turn.');},offset*1000+350);
    }
    if(button.hasAttribute('data-rx-sound')) {
      if(!soundEnabled)return;
      if(autoSource){try{autoSource.stop();autoSource.disconnect();}catch{}autoSource=null;}
      if(audio){audio.pause();audio=null;button.setAttribute('aria-pressed','false');button.textContent='♪ Play sound';return;}
      // Same-origin packaged WAV; no provider call, network credentials, or speech.
      const source=typeof options.soundUrl==='string' && options.soundUrl.startsWith('/api/apps/')?options.soundUrl:`/api/apps/${encodeURIComponent(appId)}/media/reveal.wav`;
      audio=new Audio(source);audio.volume=.45;
      button.setAttribute('aria-pressed','true');button.textContent='♪ Stop sound';
      const reset=()=>{audio=null;if(!abort.signal.aborted){button.setAttribute('aria-pressed','false');button.textContent='♪ Play sound';}};
      audio.addEventListener('ended',reset,{once:true});audio.addEventListener('error',()=>{reset();feedback('App sound unavailable.');},{once:true});
      try{await audio.play();options.onSoundPreferenceChange?.(true);}catch{reset();feedback('App sound could not play.');}
    }
  });
  if(appId==='kitchen-timers' && target.querySelector('[data-rx-due]')) {
    const update=()=>target.querySelectorAll('[data-rx-due]').forEach(node=>{const due=Number(node.dataset.rxDue);if(!Number.isFinite(due))return;const remaining=Math.max(0,due-Date.now()/1000);node.textContent=formatSeconds(remaining);const notice=node.closest('.rx-timer-card')?.querySelector('.rx-timer-browser-note');if(notice)notice.hidden=remaining>0;});
    update();tick=setInterval(update,1000);
  }
  if(reduced)target.querySelector('.rx-experience')?.classList.add('rx-reduced-motion');
  const interaction=interactions.get(options.interactionToken);
  if(interaction && interaction.appId===appId && !interaction.consumed){
    interaction.consumed=true;
    const revealAt=Date.now();
    if(result.ok!==false && interaction.enabled && soundEnabled && Date.now()-interaction.createdAt<600000){
      interaction.ready.then(buffer=>{
        // The cue belongs to this reveal only: no late playback after a slow fetch,
        // navigation, a Stop, a new request, history restore, or a background tab.
        if(!buffer || abort.signal.aborted || interaction.cancelled || !soundEnabled || !soundPreference() || Date.now()-revealAt>500 || globalThis.document?.hidden || interaction.context.state!=='running')return;
        try{stopCue();autoSource=interaction.context.createBufferSource();autoSource.buffer=buffer;const gain=interaction.context.createGain();gain.gain.value=.4;autoSource.connect(gain);gain.connect(interaction.context.destination);interaction.source=autoSource;playingCue=autoSource;autoSource.addEventListener('ended',()=>{gain.disconnect();if(playingCue===autoSource)playingCue=null;},{once:true});autoSource.start();}catch{}
      });
    }
  }
  const state={signature,destroy(){abort.abort();timeouts.forEach(clearTimeout);timeouts.clear();if(tick)clearInterval(tick);cancelExperienceInteraction(options.interactionToken);if(autoSource){try{autoSource.stop();autoSource.disconnect();}catch{}autoSource=null;}if(audio){audio.pause();audio.removeAttribute('src');audio.load();audio=null;}target.querySelectorAll('[data-rx-secret]').forEach(node=>{node.textContent='••••••••••••';});}};
  mounted.set(target,state);
  return {handled:true,destroy:()=>teardownExperience(target)};
}
