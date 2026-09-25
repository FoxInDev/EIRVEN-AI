/*
 * EIRVEN AI — 2.4.0
 * Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
 * Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
 * Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
 * EIRVEN-LICENSE-HEADER
 */
const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));
const state={identity:null,style:null,preferences:null,mail:null,telegram:null,mobile:null,camera:null,runtime:{},conversationId:localStorage.getItem('eirven_conversation')||null,sessionRestored:false,job:null,view:'home',runtimeAction:{},chatBusy:false,chatController:null,chatCancelled:false,chatTimedOut:false,chatPending:null,chatQueued:false,attachments:[],models:null,mailSetupOpen:false,telegramSetupOpen:false};
function apiErrorText(detail,raw,status){
  if(typeof detail==='string'&&detail.trim())return detail;
  if(Array.isArray(detail)){
    const parts=detail.map(d=>{
      if(typeof d==='string')return d;
      const where=Array.isArray(d&&d.loc)?d.loc.filter(x=>x!=='body').join(' → '):'';
      const msg=(d&&(d.msg||d.message))||'';
      return [where,msg].filter(Boolean).join(': ');
    }).filter(Boolean);
    if(parts.length)return parts.join('; ');
  }
  if(detail&&typeof detail==='object'){
    const msg=detail.msg||detail.message||detail.error;
    if(typeof msg==='string'&&msg.trim())return msg;
    try{return JSON.stringify(detail)}catch{}
  }
  return (typeof raw==='string'&&raw.trim())?raw:`HTTP ${status}`;
}
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts});if(!r.ok){const raw=await r.text();let detail=raw;try{detail=JSON.parse(raw).detail}catch{}throw new Error(apiErrorText(detail,raw,r.status))}const ct=r.headers.get('content-type')||'';return ct.includes('application/json')?r.json():r.text()}
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function configuredName(){return 'Эрви'}
function wakeName(r=state.runtime){return String(r?.wake_phrase||configuredName()).trim()||configuredName()}
function applyAssistantName(){const name='Эрви';document.title=name;$$('.assistant-name-ref').forEach(el=>el.textContent=name);$('#stage-title').textContent=state.view==='home'?name:$('#stage-title').textContent;$('.brand-orb').title=name;$('.brand-orb').setAttribute('aria-label',name);$('#main-orb').setAttribute('aria-label',`Сфера ${name}`);$('#message-input').placeholder='Напиши сообщение…';if($('#onboard-intro-name'))$('#onboard-intro-name').textContent=name}
function setView(name){state.view=name;$$('.view').forEach(v=>v.classList.toggle('active',v.id===`view-${name}`));$$('.nav-button').forEach(b=>b.classList.toggle('active',b.dataset.view===name));$('#stage-title').textContent=name==='settings'?'Настройки':'Эрви';document.body.dataset.view=name;if(name==='settings')loadPreferences()}
function openSettingsTab(tab='general'){setView('settings');const button=$(`[data-settings-tab="${tab}"]`);if(button)setTimeout(()=>button.click(),0)}
function applyAgentRoute(route={}){if(route.open_settings_tab)openSettingsTab(route.open_settings_tab)}
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
function renderMessage(node,text){const value=String(text||'');if(window.EirvenMarkdown?.renderInto)window.EirvenMarkdown.renderInto(node,value);else node.textContent=value;node.dataset.rawText=value;return value}
function addMessage(role,text,extra=''){const box=$('#messages'),node=document.createElement('div');node.className=`message ${role} ${extra}`;renderMessage(node,text);maybeAttachTeachButton(node,role,text);box.append(node);box.scrollTop=box.scrollHeight;return node}
function maybeAttachTeachButton(node,role,text,route={}){
  // Показываем кнопку только под сообщением о неудаче действия — там, где
  // человек и решает, стоит ли показать шаги руками.
  if(role!=='ervi'&&role!=='assistant')return;
  if(node.querySelector('.teach-bar'))return;
  if(!route.teaching_available&&!/обуч(?:и|ить) выполнению/i.test(String(text||'')))return;
  const bar=document.createElement('div');bar.className='teach-bar';
  const btn=document.createElement('button');btn.type='button';btn.className='glass-action';
  btn.textContent='Обучить выполнению';
  btn.onclick=async()=>{
    btn.disabled=true;
    try{
      const r=await api('/api/teaching/start',{method:'POST',body:JSON.stringify({goal:route.teaching_goal||''})});
      addMessage('ervi',r.message||'Записываю. Покажи действия и скажи «готово».');
      if(r.ok)showTeachFinish();
    }catch(e){addMessage('ervi',e.message)}
    finally{btn.disabled=false}
  };
  bar.append(btn);node.append(bar);
}
function showTeachFinish(){
  if(document.querySelector('[data-teach-finish]'))return;
  const bar=document.createElement('div');bar.className='teach-floating';bar.dataset.teachFinish='1';
  const label=document.createElement('span');label.textContent='Записываю твои действия…';
  const done=document.createElement('button');done.type='button';done.className='glass-action';done.textContent='Готово';
  done.onclick=async()=>{
    try{const r=await api('/api/teaching/finish',{method:'POST'});addMessage('ervi',r.message||'Записала.')}
    catch(e){addMessage('ervi',e.message)}
    finally{bar.remove()}
  };
  bar.append(label,done);document.body.append(bar);
}
function renderOutputFiles(node,files=[]){const values=Array.isArray(files)?files:[];if(!values.length)return;const old=node.nextElementSibling;if(old?.classList?.contains('output-files'))old.remove();const box=document.createElement('div');box.className='output-files';values.slice(0,8).forEach(file=>{const a=document.createElement('a');a.href=file.url||'#';a.download=file.name||'';a.textContent=`📎 ${file.name||'Файл'}`;const size=Number(file.size)||0;if(size)a.title=`${Math.max(1,Math.round(size/1024))} КБ`;box.append(a)});node.after(box)}
async function restoreConversationSession(){try{const session=await api('/api/session'),conv=session?.conversation;state.sessionRestored=true;if(session?.identity)state.identity=session.identity;if(session?.style)state.style=session.style;if(conv?.id){state.conversationId=String(conv.id);localStorage.setItem('eirven_conversation',state.conversationId);return state.conversationId}state.conversationId=null;localStorage.removeItem('eirven_conversation');return null}catch{state.sessionRestored=true;return state.conversationId}}
async function ensureConversation(force=false){if(!force&&state.sessionRestored&&state.conversationId)return state.conversationId;if(!force){const restored=await restoreConversationSession();if(restored)return restored}const conv=await api('/api/conversations',{method:'POST',body:JSON.stringify({title:configuredName(),mode:'Друг'})});state.conversationId=String(conv.id);state.sessionRestored=true;localStorage.setItem('eirven_conversation',state.conversationId);return state.conversationId}
async function loadHistory(){
  if(!state.conversationId)return;
  try{
    let conv;
    try{conv=await api(`/api/conversations/${state.conversationId}`)}
    catch{
      // A reinstall can invalidate only the browser's local id while the durable
      // conversation still exists in EIRVEN's local database. Recover the newest
      // non-empty conversation instead of silently replacing the user's history.
      const rows=await api('/api/conversations?limit=50');
      const recovered=(rows||[]).find(item=>Number(item.message_count||0)>0);
      if(!recovered?.id)throw new Error('Чат не найден');
      state.conversationId=String(recovered.id);localStorage.setItem('eirven_conversation',state.conversationId);await api('/api/session/conversation',{method:'PUT',body:JSON.stringify({conversation_id:state.conversationId})});
      conv=await api(`/api/conversations/${state.conversationId}`);
    }
    $('#messages').innerHTML='';
    (conv.messages||[]).slice(-50).forEach(m=>{const node=addMessage(m.role==='user'?'user':'assistant',m.content||'');renderOutputFiles(node,m.metadata?.files||[])})
  }catch{
    state.conversationId=null;localStorage.removeItem('eirven_conversation');
  }
}
function autosize(){const input=$('#message-input');input.style.height='auto';input.style.height=`${Math.min(180,Math.max(76,input.scrollHeight))}px`}
function setChatBusy(busy){state.chatBusy=busy;document.body.classList.toggle('chat-thinking',busy);const button=$('#send-message');button.textContent=busy?'Стоп':'Отправить';button.setAttribute('aria-label',busy?'Остановить ответ':'Отправить');button.classList.toggle('danger-soft',busy)}
function routeLabel(route={}){const named={mail_check:'Читаю почту',mail_send:'Готовлю письмо',telegram_monitor:'Настраиваю Telegram',telegram_send:'Пишу в Telegram',media_control:'Управляю плеером',pc_shutdown_confirmation:'Готовлю выключение',reminder:'Ставлю напоминание',video_edit:'Монтирую видео',vision_direct:'Смотрю изображение',open_application:'Открываю приложение',teaching_started:'Записываю твои действия',creator_identity:'Отвечаю'};if(route.action&&named[route.action])return named[route.action];if(route.think)return'Думаю глубже';if(route.needs_user)return'Уточняю следующий шаг';if(route.action&&route.action!=='chat'&&route.action!=='instant')return'Выполняю и проверяю';return'Формирую ответ'}function resultLabel(route={},metrics={}){const seconds=Number(metrics.total_seconds);const timing=Number.isFinite(seconds)&&seconds>.05?` · ${seconds<10?seconds.toFixed(1):Math.round(seconds)} с`:'';if(route.needs_user)return`Нужен ваш выбор${timing}`;if(route.verified===true)return`Готово · проверено${timing}`;if(route.completed===true&&route.verified===false)return`Выполнено · подтверждение не получено${timing}`;if(route.completed===false||/failed|error|unverified/.test(String(route.action||'')))return`Не выполнено${timing}`;return`Ответ готов${timing}`}function consumeStreamPacket(packet,node,current){let full=current;for(const line of packet.split('\n')){if(!line.startsWith('data:'))continue;let event;try{event=JSON.parse(line.slice(5).trim())}catch{continue}if(event.type==='start'){const route=event.route||{};if(!node.dataset.bridge){renderMessage(node,route.think?'Думаю глубже…':route.action&&route.action!=='chat'?'Выполняю и проверяю…':'Отвечаю…');}$('#chat-model-status').textContent=routeLabel(route)}else if(event.type==='token'){full=event.full||full+(event.content||'');renderMessage(node,node.dataset.bridge&&full&&full!==node.dataset.bridge?node.dataset.bridge+'\n\n'+full:full);node.classList.remove('pending')}else if(event.type==='error'){full=event.message||'Не удалось получить ответ';renderMessage(node,full);node.classList.remove('pending');$('#chat-model-status').textContent='Ошибка выполнения';renderChoicePanel(node,event.route||{})}else if(event.type==='bridge'){if(event.text){node.dataset.bridge=event.text;renderMessage(node,event.text);$('#chat-model-status').textContent='Работаю над задачей…';}}else if((event.type==='done'||event.type==='final')){full=event.answer||full;if(node.dataset.bridge&&full&&full!==node.dataset.bridge){full=node.dataset.bridge+'\n\n'+full;}renderMessage(node,full||'Исполнитель не вернул подтверждённый результат.');node.classList.remove('pending');$('#chat-model-status').textContent=resultLabel(event.route||{},event.metrics||{});applyAgentRoute(event.route||{});renderChoicePanel(node,event.route||{});renderOutputFiles(node,event.files||event.route?.files||[]);maybeAttachTeachButton(node,'assistant',full,event.route||{})}}return full}
async function streamChat(payload,node,controller){let inactivity=0;const arm=()=>{clearTimeout(inactivity);inactivity=setTimeout(()=>{state.chatTimedOut=true;controller.abort()},180000)};arm();try{const response=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal});if(!response.ok)throw new Error((await response.text())||`HTTP ${response.status}`);if(!response.body)throw new Error('Поток ответа недоступен');const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',full='';for(;;){const {value,done}=await reader.read();if(done)break;arm();buffer+=decoder.decode(value,{stream:true});let split;while((split=buffer.indexOf('\n\n'))>=0){full=consumeStreamPacket(buffer.slice(0,split),node,full);buffer=buffer.slice(split+2)}}buffer+=decoder.decode();if(buffer.trim())full=consumeStreamPacket(buffer,node,full);if(!full.trim())throw new Error('Поток завершился без ответа. Повтори команду.');node.classList.remove('pending');return full}finally{clearTimeout(inactivity)}}
async function stopChat(){if(!state.chatBusy)return;state.chatCancelled=true;state.chatController?.abort();const id=state.conversationId;if(id){try{await api(`/api/chat/${encodeURIComponent(id)}/stop`,{method:'POST'})}catch{}}}
function renderAttachments(){const box=$('#attachment-strip');box.innerHTML='';box.hidden=!state.attachments.length;state.attachments.forEach((item,i)=>{const chip=document.createElement('span');chip.className='attachment-chip';chip.textContent=`${item.name||'Файл'} `;const b=document.createElement('button');b.type='button';b.textContent='×';b.title='Убрать вложение';b.onclick=()=>{state.attachments.splice(i,1);renderAttachments()};chip.append(b);box.append(chip)})}
async function uploadFiles(files){if(!files?.length)return;await ensureConversation();for(const file of Array.from(files)){const form=new FormData();form.append('file',file,file.name);form.append('conversation_id',state.conversationId);const r=await fetch('/api/uploads',{method:'POST',body:form});if(!r.ok)throw new Error((await r.text())||`Не удалось загрузить ${file.name}`);state.attachments.push(await r.json());renderAttachments()}}
async function sendMessage(){const input=$('#message-input');if(state.chatBusy){const hasNext=!!(input.value.trim()||state.attachments.length);state.chatQueued=hasNext;await stopChat();if(hasNext)$('#chat-model-status').textContent='Перехожу к новой команде';return}const typed=input.value.trim();if(!typed&&!state.attachments.length)return;dismissChoicePanels(true);const message=typed||'Проанализируй прикреплённые файлы.';const sentAttachments=[...state.attachments];const attachmentIds=sentAttachments.map(x=>x.id);const controller=new AbortController();state.chatController=controller;state.chatCancelled=false;state.chatTimedOut=false;setChatBusy(true);let pending=null;let slow=0;try{await ensureConversation();input.value='';state.attachments=[];renderAttachments();autosize();addMessage('user',typed||`📎 ${sentAttachments.map(x=>x.name).join(', ')}`);pending=addMessage('assistant','Эрви обдумывает ответ','pending');pending.setAttribute('aria-label','Эрви обдумывает ответ');state.chatPending=pending;slow=setTimeout(()=>{if(state.chatBusy&&pending.classList.contains('pending'))pending.setAttribute('aria-label','Эрви продолжает выполнять задачу')},12000);await streamChat({message,conversation_id:state.conversationId,mode:'Друг',model:'auto',attachment_ids:attachmentIds,auto_execute:true},pending,controller)}catch(e){if(sentAttachments.length){state.attachments=[...sentAttachments,...state.attachments];renderAttachments()}if(!pending)pending=addMessage('assistant','','pending');pending.classList.remove('pending');if(state.chatCancelled)pending.textContent='Ответ остановлен новой командой.';else if(state.chatTimedOut||e?.name==='AbortError')pending.textContent='Ответ занял слишком много времени. Повтори команду — сохранённый прогресс не потерян.';else{pending.textContent=`Не получилось ответить: ${e.message}`;renderChoicePanel(pending,{ui_question:'Что сделать?',ui_choices:[{label:'Найти в поиске',value:`Найди в интернете ответ на вопрос: ${message}`},{label:'Повторить',value:'Повтори мой предыдущий вопрос'}]})}}finally{clearTimeout(slow);if(state.chatController===controller)state.chatController=null;state.chatPending=null;setChatBusy(false);const runNext=state.chatQueued;state.chatQueued=false;if(runNext)queueMicrotask(()=>sendMessage())}}
function dismissChoicePanels(force=false){const input=$('#message-input');if(!force&&!input?.value.trim())return;const dock=$('#sphere-intervention');if(dock){dock.hidden=true;dock.innerHTML=''}$$('.choice-panel').forEach(panel=>panel.remove());syncOrbLayout()}
$('#send-message').onclick=sendMessage;$('#attach-file').onclick=()=>$('#file-input').click();$('#file-input').addEventListener('change',async e=>{try{await uploadFiles(e.target.files)}catch(err){addMessage('assistant',`Файл: ${err.message}`)}finally{e.target.value=''}});const composer=$('.composer');composer.addEventListener('dragover',e=>{e.preventDefault();composer.classList.add('dragging')});composer.addEventListener('dragleave',()=>composer.classList.remove('dragging'));composer.addEventListener('drop',async e=>{e.preventDefault();composer.classList.remove('dragging');try{await uploadFiles(e.dataTransfer.files)}catch(err){addMessage('assistant',`Файл: ${err.message}`)}});$('#message-input').addEventListener('input',()=>{autosize();dismissChoicePanels()});$('#message-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}});$('#new-chat').onclick=async()=>{if(state.chatBusy)await stopChat();state.conversationId=null;localStorage.removeItem('eirven_conversation');$('#messages').innerHTML='';await ensureConversation(true)};

function micEnabled(){return state.preferences?.voice_wake_enabled!==false}function runtimeLabel(r){if(!micEnabled())return 'микрофон выключен';if(r.onboarding_complete===false||r.state==='onboarding')return 'знакомлюсь с тобой';if(r.error)return 'микрофон недоступен';if(r.state==='warming'||r.interactive_ready===false)return 'запускаюсь';if(r.speaking)return 'говорю';if(r.state==='hearing')return 'слушаю';if(r.state==='recognizing')return 'понимаю';if(r.state==='thinking')return 'думаю';return 'слушаю — говори'}
function updateMicUI(r=state.runtime){const enabled=micEnabled(),button=$('#home-mic-toggle');if(!button)return;const unavailable=enabled&&!!r?.error;const starting=enabled&&!unavailable&&(r?.running===false||r?.state==='warming'||!r);button.classList.toggle('on',enabled&&!unavailable);button.classList.toggle('error',unavailable);button.classList.toggle('starting',starting);button.setAttribute('aria-pressed',String(enabled));button.setAttribute('aria-label',enabled?'Выключить микрофон':'Включить микрофон');button.querySelector('span').textContent=!enabled?'Микрофон выключен':unavailable?'Микрофон недоступен':starting?'Подождите…':'Микрофон включён';$('#home-mic-hint').textContent=!enabled?'Включи микрофон — после этого можно говорить без слова-пробуждения':unavailable?'Микрофон включён в настройках, но голосовой модуль сейчас недоступен':starting?'Микрофон запускается — подожди пару секунд, потом говори. Пока можно писать в чат':'Говори обычной фразой — специальное слово не нужно'}
function updateOrb(r){const orb=$('#main-orb');if(window.eirvenOrb&&window.eirvenOrb.pulse){window.eirvenOrb.pulse(r.speaking?Math.min(1,0.35+(r.speech_level||0)*6):(r.state==='thinking'?0.3:(r.state==='hearing'?0.2:0)));}['active','hearing','speaking','thinking','onboarding','mood-happy','mood-sad','mood-curious','mood-tired','mood-warm','mood-concerned'].forEach(c=>orb.classList.remove(c));if(r.onboarding_complete===false||r.state==='onboarding')orb.classList.add('onboarding');if(r.session_active||r.state==='armed')orb.classList.add('active');if(r.state==='hearing'||r.state==='recognizing')orb.classList.add('hearing');if(r.state==='thinking')orb.classList.add('thinking');if(r.speaking)orb.classList.add('speaking');const emotion=String(r.speaking_emotion||r.response_emotion||r.mood?.emotion||'').toLowerCase(),mood=emotion==='amused'||emotion==='proud'||emotion==='energetic'?'happy':emotion==='empathetic'||emotion==='calm'||emotion==='quiet'?'warm':emotion==='strict'?'concerned':emotion;if(['happy','sad','curious','tired','warm','concerned'].includes(mood))orb.classList.add(`mood-${mood}`);const level=Math.min(.22,Number(r.input_level)||0),speech=Math.max(0,Math.min(1,Number(r.speech_level)||0));document.documentElement.style.setProperty('--voice-level',String(level));document.documentElement.style.setProperty('--speech-level',String(speech));document.documentElement.style.setProperty('--arm',String(Math.max(0,Math.min(1,(Number(r.session_seconds_remaining)||0)/5))));noteOrbActivity(r)}
const RESTING_AFTER_MS=240000,ACTIVE_STATES=['hearing','recognizing','thinking','armed'];
let lastOrbActivityAt=Date.now();
function noteOrbActivity(r){if(r.speaking||ACTIVE_STATES.includes(r.state)||r.session_active||state.chatBusy)lastOrbActivityAt=Date.now()}
setInterval(()=>{const orb=$('#main-orb');if(!orb)return;const quiet=!orb.classList.contains('hearing')&&!orb.classList.contains('thinking')&&!orb.classList.contains('speaking')&&!orb.classList.contains('active')&&!orb.classList.contains('onboarding');orb.classList.toggle('resting',quiet&&Date.now()-lastOrbActivityAt>RESTING_AFTER_MS)},4000);
async function pollRuntime(){try{const r=await api('/api/voice/runtime');state.runtime=r;updateOrb(r);updateMicUI(r);const label=runtimeLabel(r);$('#voice-state').textContent=label;$('#heard-text').textContent=['hearing','recognizing','thinking'].includes(r.state)?(r.last_text||''):'';const chip=$('#runtime-chip');chip.className='runtime-chip '+(r.error?'error':(r.speaking||r.session_active||['hearing','recognizing','thinking','armed'].includes(r.state))?'active':r.running?'live':'');chip.querySelector('b').textContent=label;$('#health-dot').className='health-dot '+((!micEnabled()||r.running)&&!r.error?'ok':'bad')}catch{$('#health-dot').className='health-dot bad'}}
function scheduleRuntimePoll(){pollRuntime().finally(()=>{setTimeout(scheduleRuntimePoll,state.runtime?.speaking?90:350)})}scheduleRuntimePoll();

function humanActivity(rt){if(!rt||!rt.cancellable)return'';let step=String(rt.step||rt.action||'').trim(),goal=String(rt.goal||'').trim();if(step&&!["готово","idle","остановлено"].includes(step.toLowerCase()))return step.replace(/\.$/,'')+'…';if(goal)return 'Занимаюсь: '+goal.slice(0,90)+(goal.length>90?'…':'');return''}

/* Эмоции Эрви на главной сфере выражает само лицо — новые стеклянные глаза и рот плюс цвет свечения по макету. Картинку сферы не меняем: у картинок эмоций лица нарисованы внутри, и поверх стеклянных глаз получалось два лица сразу. Желейные картинки эмоций показывает сфера на рабочем столе, где накладного лица нет. */
const EMOTIONS=['joy','surprise','thinking','focused','laugh','sleep'];
let currentEmotion='';
function applyEmotion(name){try{if(!EMOTIONS.includes(name)||name===currentEmotion)return;const orb=document.getElementById('main-orb');if(!orb)return;currentEmotion=name;EMOTIONS.forEach(n=>orb.classList.remove('emotion-'+n));orb.classList.add('emotion-'+name)}catch{}}
async function pollAction(){try{const rt=await api('/api/runtime');state.runtimeAction=rt;applyEmotion(rt?.emotion);const text=humanActivity(rt),el=$('#activity-caption'),bar=$('#home-task-progress');el.textContent=text;el.classList.toggle('visible',!!text);bar.hidden=!rt?.cancellable}catch{}}
setInterval(pollAction,500);pollAction();

const STYLE_PRESETS={balanced:{directness:4,humor:'сдержанный живой тон, почти без шуток',emotional_support:true,answer_length:'средняя',custom_rules:'Говори естественно, спокойно и по-человечески. Без канцелярита, без лишнего пафоса. Подстраивай тон под ситуацию.'},friendly:{directness:3,humor:'тёплый дружеский юмор, лёгкий и мягкий',emotional_support:true,answer_length:'средняя',custom_rules:'Говори теплее, мягче и поддерживающе. Чаще используй дружелюбные формулировки и живые человеческие реплики.'},brief:{directness:5,humor:'без юмора',emotional_support:false,answer_length:'короткая',custom_rules:'Отвечай коротко, собранно и по делу. Не растекайся мыслью, не добавляй шутки, не проговаривай очевидное.'},free:{directness:4,humor:'заметный живой юмор, игривый, лёгкий, уместный',emotional_support:true,answer_length:'средняя',custom_rules:'Говори свободнее и живее. Допускай короткие уместные шутки, дружеские подколы и более эмоциональные формулировки, но без клоунады.'}};
function presetFromStyle(st){const h=String(st?.humor||'');if(st?.answer_length==='короткая'||Number(st?.directness)>=5)return'brief';if(h.includes('игрив'))return'free';if(h.includes('друж'))return'friendly';return'balanced'}
function setPresetButtons(root,value){if(!root)return;root.querySelectorAll('[data-style-preset]').forEach(button=>button.classList.toggle('active',button.dataset.stylePreset===value))}
async function saveStylePreset(name){const current=await api('/api/style');const patch=STYLE_PRESETS[name]||STYLE_PRESETS.balanced;state.style=await api('/api/style',{method:'PUT',body:JSON.stringify({...current,...patch,owner_name:state.identity?.user_address||current.owner_name})});setPresetButtons($('#communication-style'),name);setPresetButtons($('#onboard-style'),name);onboardingDraft.style=name}
async function loadIdentity(){const id=await api('/api/identity');state.identity=id;$('#user-address').value=id.user_address||'';$('#onboard-user-name').value=id.user_address||'';applyAssistantName();return id}
async function saveIdentity(patch){const id=await api('/api/identity',{method:'PUT',body:JSON.stringify(patch)});state.identity=id;await loadIdentity();return id}
async function loadStyle(){try{state.style=await api('/api/style');const preset=presetFromStyle(state.style);setPresetButtons($('#communication-style'),preset);setPresetButtons($('#onboard-style'),preset);onboardingDraft.style=preset}catch{}}
$('#user-address').addEventListener('change',e=>saveIdentity({user_address:e.target.value}));$$('#communication-style [data-style-preset]').forEach(button=>button.addEventListener('click',()=>saveStylePreset(button.dataset.stylePreset)));$$('#onboard-style [data-style-preset]').forEach(button=>button.addEventListener('click',()=>{onboardingDraft.style=button.dataset.stylePreset;persistOnboardingDraft();setPresetButtons($('#onboard-style'),onboardingDraft.style)}));

function applyAppearance(p={}){document.body.classList.toggle('sphere-motion-off',p.sphere_motion===false);document.body.classList.remove('sphere-soft','sphere-balanced','sphere-vivid');document.body.classList.add(`sphere-${p.sphere_intensity||'vivid'}`)}
async function loadModels(){const box=$('#models-list');if(!box)return;try{const value=await api('/api/models');state.models=value;const names=Array.isArray(value.models)?value.models:[],preferred=value.recommended?.main||'';const ready=!!names.find(name=>String(name).toLowerCase()===String(preferred).toLowerCase())||!!names.length;$('#active-model-name').textContent=ready?'Готово для этого устройства':'Завершаю подготовку';$('#active-model-mode').textContent='Скорость и качество выбираются автоматически';$('#model-health-dot').classList.toggle('ok',ready);box.innerHTML=`<div class="model-row"><b>${ready?'Оптимальный режим активен':'Подготовка продолжится автоматически'}</b><small>${ready?'Можно работать':'Прогресс сохранится после перезапуска'}</small></div>`;return value}catch(e){$('#active-model-name').textContent='Временно недоступно';$('#active-model-mode').textContent='Повтори запуск Эрви';box.textContent='Ничего настраивать вручную не нужно.';return null}}
async function loadPreferences(){try{const p=await api('/api/preferences');state.preferences=p;$('#voice-output-volume').value=Math.round(Number(p.voice_output_volume??.82)*100);$('#voice-output-volume-value').textContent=`${$('#voice-output-volume').value}%`;$('#ui-language').value=p.language||'ru';$('#autostart-enabled').checked=!!p.autostart;$('#notifications-enabled').checked=p.notifications_enabled!==false;$('#mini-mode-enabled').checked=p.mini_mode!==false;$('#sphere-motion').checked=p.sphere_motion!==false;$('#sphere-intensity').value=p.sphere_intensity||'vivid';$('#desktop-comments-enabled').checked=p.desktop_comments_enabled!==false;if($('#proactive-output-mode'))$('#proactive-output-mode').value=p.proactive_output_mode||'voice';if($('#confirmation-mode'))$('#confirmation-mode').value=p.confirmation_mode||'critical';if($('#voice-wake-enabled'))$('#voice-wake-enabled').checked=p.voice_wake_enabled!==false;if($('#desktop-control-enabled'))$('#desktop-control-enabled').checked=!!p.desktop_control_enabled;$('#update-channel').value=p.update_channel||'stable';$('#auto-update-enabled').checked=!!p.auto_update_enabled;const d=p.distribution||{};$('#delivery-channel').textContent=String(d.claimed_channel||'standard').toUpperCase();$('#version-caption').textContent=`EIRVEN ${p.version||'2.0.0'} · ${p.build||'r67-universal-engine'}`;$('#version-pill').textContent=p.build||'r67-universal-engine';applyAppearance(p);updateMicUI();return p}catch(e){return null}}
async function savePreferences(patch){try{const p=await api('/api/preferences',{method:'PUT',body:JSON.stringify(patch)});state.preferences=p;applyAppearance(p);return p}catch(e){addMessage('assistant',`Настройки: ${e.message}`);throw e}}
async function loadActionJournal(){const box=$('#action-journal');if(!box)return;box.innerHTML='<span>Загружаю…</span>';try{const rows=await api('/api/activity-journal?limit=100');box.innerHTML=rows.length?rows.map(row=>`<div class="action-log ${row.status==='failed'?'bad':''}"><div class="action-log-head"><b>${escapeHtml(row.title||'Действие')}${Number(row.repeat_count||1)>1?` ×${Number(row.repeat_count)}`:''}</b><span class="log-status">${row.status==='verified'?'Проверено':row.status==='failed'?'Ошибка':'Выполнено'}</span></div><small>${escapeHtml(row.summary||'')}${row.reason?` · ${escapeHtml(row.reason)}`:''}</small></div>`).join(''):'<span>Журнал пока пуст. После действий здесь появятся результат и причина сбоя.</span>'}catch(e){box.innerHTML=`<span>Журнал недоступен: ${escapeHtml(e.message)}</span>`}}
function maskEmail(value=''){const s=String(value||'');const [name,domain]=s.split('@');if(!domain)return s;return `${name.slice(0,2)}${name.length>2?'•••':''}@${domain}`}function setMailSetup(open){state.mailSetupOpen=!!open;const panel=$('#mail-setup-panel');if(panel)panel.hidden=!state.mailSetupOpen;const button=$('#mail-edit');if(button)button.textContent=state.mailSetupOpen?'Скрыть настройку':(state.mail?.configured?'Изменить подключение':'Подключить почту')}async function loadMail(){const status=$('#mail-status');if(!status)return;try{const value=await api('/api/mail/status');state.mail=value;status.textContent=value.configured?`Почта готова: ${maskEmail(value.email)}. Пароль защищён через ${value.secret_storage}; повторный ввод не нужен.`:'Почта не подключена. Это необязательно — Эрви продолжит работать без доступа к письмам.';$('#mail-email').value=value.email||'';$('#mail-imap-host').value=value.imap_host||'';$('#mail-imap-port').value=value.imap_port||993;$('#mail-smtp-host').value=value.smtp_host||'';$('#mail-smtp-port').value=value.smtp_port||587;$('#mail-imap-ssl').checked=value.imap_ssl!==false;$('#mail-smtp-starttls').checked=value.smtp_starttls!==false;$('#mail-auto-spam').checked=!!value.auto_move_obvious_spam;$('#mail-disconnect').hidden=!value.configured;setMailSetup(state.mailSetupOpen);return value}catch(e){status.textContent=`Почта: ${e.message}`;return null}}
const MAIL_PROVIDERS=[{domains:['yandex.ru','ya.ru','yandex.com'],name:'Яндекс',imap:'imap.yandex.ru',smtp:'smtp.yandex.ru'},{domains:['mail.ru','inbox.ru','bk.ru','list.ru'],name:'Mail.ru',imap:'imap.mail.ru',smtp:'smtp.mail.ru'},{domains:['gmail.com','googlemail.com'],name:'Gmail',imap:'imap.gmail.com',smtp:'smtp.gmail.com'},{domains:['outlook.com','hotmail.com','live.com'],name:'Outlook',imap:'outlook.office365.com',smtp:'smtp-mail.outlook.com'}];
function fillMailProvider(){const email=$('#mail-email').value.trim().toLowerCase(),domain=email.split('@').pop()||'',provider=MAIL_PROVIDERS.find(x=>x.domains.includes(domain)),hint=$('#mail-provider-hint');if(!email.includes('@')){hint.textContent='Сначала введи полный email.';$('#mail-email').focus();return false}if(!provider){hint.textContent='Этого провайдера нет в автосписке. Возьми IMAP/SMTP из его официальной справки или спроси Эрви.';return false}$('#mail-imap-host').value=provider.imap;$('#mail-imap-port').value=993;$('#mail-smtp-host').value=provider.smtp;$('#mail-smtp-port').value=587;$('#mail-imap-ssl').checked=true;$('#mail-smtp-starttls').checked=true;hint.textContent=`${provider.name}: IMAP/SMTP подставлены. Осталось создать в почте пароль приложения.`;return true}
$$('[data-settings-tab]').forEach(b=>b.onclick=()=>{$$('[data-settings-tab]').forEach(x=>x.classList.toggle('active',x===b));$$('[data-settings-panel]').forEach(x=>x.classList.toggle('active',x.dataset.settingsPanel===b.dataset.settingsTab));if(b.dataset.settingsTab==='updates')checkUpdates(false);if(b.dataset.settingsTab==='models')loadModels();if(b.dataset.settingsTab==='telegram')loadTelegram();if(b.dataset.settingsTab==='general')refreshTelegramAutostart();if(b.dataset.settingsTab==='video')loadVideo();if(b.dataset.settingsTab==='mobile')loadMobile();if(b.dataset.settingsTab==='security')loadActionJournal();if(b.dataset.settingsTab==='mail')loadMail();if(b.dataset.settingsTab==='camera')loadCamera()});
function renderMobileQr(url){const box=$('#mobile-download-qr'),link=$('#mobile-download-link'),detail=$('#mobile-apk-detail');box.innerHTML='';link.hidden=true;link.removeAttribute('href');if(!url){box.innerHTML='<span>QR пока недоступен</span>';detail.textContent='APK или Wi‑Fi адрес не найден. Перезапусти EIRVEN после подключения к домашней сети.';return false}try{if(typeof qrcode!=='function')throw new Error('QR-модуль не загрузился');const qr=qrcode(0,'M');qr.addData(url,'Byte');qr.make();box.innerHTML=qr.createSvgTag(5,20,'QR-код страницы установки','Установить EIRVEN Mobile');link.href=url;link.hidden=false;detail.textContent='На телефоне сначала появится подтверждение связи с компьютером, затем кнопка скачивания APK.';return true}catch(e){box.innerHTML='<span>Не удалось создать QR</span>';link.href=url;link.hidden=false;detail.textContent=`QR: ${e.message}. Используй кнопку открытия страницы.`;return false}}
function mobileOptionLabel(option){const kind={wifi:'Wi‑Fi',ethernet:'Кабель',virtual:'VPN/виртуальная',network:'Сеть'}[option.kind]||'Сеть';return `${kind} · ${option.interface||option.ip||''} · ${option.ip||''}`}
function renderMobileNetworkDetail(value,selected){const detail=$('#mobile-network-detail'),option=(value.address_options||[]).find(item=>item.url===selected),virtualWarning=option?.kind==='virtual'?' Кроме того, выбран адрес VPN/виртуальной сети — телефон обычно его не видит. Выбери Wi‑Fi ниже.':'';detail.className='mobile-network-detail';if(value.firewall_ready===false){detail.classList.add('error');detail.textContent=(value.detail||'Windows Firewall не разрешил вход с телефона. Перезапусти EIRVEN через ярлык и подтверди UAC.')+virtualWarning;return}if(value.firewall_ready==null){detail.classList.add('error');detail.textContent=(value.detail||'Не удалось подтвердить правило Windows Firewall. Перезапусти EIRVEN через ярлык.')+virtualWarning;return}if(option?.kind==='virtual'){detail.classList.add('error');detail.textContent='Выбран адрес VPN/виртуальной сети — телефон обычно его не видит. Выбери кнопку Wi‑Fi ниже.';return}detail.classList.add('ready');const adapter=option?.interface?`Выбран интерфейс «${option.interface}» (${option.ip}). `:'';detail.textContent=adapter+(value.detail||'Доступ разрешён только устройствам этого локального сегмента.')}
function chooseMobileAddress(url){const value=state.mobile||{},selected=String(url||'');$('#mobile-address').textContent=selected||'Адрес локальной сети не найден';$$('.mobile-address-option').forEach(button=>button.classList.toggle('active',button.dataset.url===selected));const qrReady=renderMobileQr(selected?`${selected}/mobile/install`:'');renderMobileNetworkDetail(value,selected);return qrReady}
function renderMobileAddressOptions(value){const box=$('#mobile-address-options'),options=Array.isArray(value.address_options)?value.address_options:[];box.innerHTML='';box.hidden=options.length<2;for(const option of options){const button=document.createElement('button');button.type='button';button.className=`mobile-address-option ${option.kind==='virtual'?'virtual':''}`;button.dataset.url=option.url;button.textContent=mobileOptionLabel(option);button.title=option.warning||`Использовать ${option.url}`;button.onclick=()=>{const ready=chooseMobileAddress(option.url);$('#mobile-status').textContent=ready?'QR обновлён для выбранного адреса. Попробуй открыть его камерой телефона.':'Адрес выбран, но QR не создался — используй ссылку.'};box.append(button)}}
async function loadMobile(){const status=$('#mobile-status');status.textContent='Проверяю локальную сеть…';try{const value=await api('/api/mobile/config');state.mobile=value;$('#mobile-token').textContent=value.token||'—';renderMobileAddressOptions(value);const selected=value.preferred_address||'';const qrReady=chooseMobileAddress(selected);const preferred=(value.address_options||[]).find(item=>item.url===selected);status.textContent=!value.lan_enabled?'Доступ с телефона выключен в .env':!value.apk_available?'APK не включён в эту сборку':value.firewall_ready===false?'Нужно один раз подтвердить запрос Windows для доступа с телефона. Перезапусти EIRVEN через ярлык и согласись с UAC.':value.firewall_ready==null?'Не удалось проверить Windows Firewall. Перезапусти EIRVEN через ярлык.':preferred?.kind==='virtual'?'Нашла только виртуальный/VPN-адрес. Отключи VPN или выбери Wi‑Fi адрес ниже.':selected?(qrReady?'Готово: Wi‑Fi адрес проверен, отсканируй QR':'Адрес готов; скачай APK кнопкой'):'Не нашла Wi‑Fi адрес. Подключи компьютер к сети и нажми «Обновить Wi‑Fi адрес».';return value}catch(e){state.mobile=null;renderMobileQr('');$('#mobile-network-detail').className='mobile-network-detail error';$('#mobile-network-detail').textContent=`Не удалось проверить сеть: ${e.message}`;status.textContent=`Не удалось получить данные: ${e.message}`;return null}}
async function copyMobile(selector,label){const value=$(selector).textContent.trim();if(!value||value.includes('не найден'))return;try{await navigator.clipboard.writeText(value);$('#mobile-status').textContent=`${label} скопирован`}catch{const area=document.createElement('textarea');area.value=value;document.body.append(area);area.select();document.execCommand('copy');area.remove();$('#mobile-status').textContent=`${label} скопирован`}}
$('#copy-mobile-address').onclick=()=>copyMobile('#mobile-address','Адрес');$('#copy-mobile-token').onclick=()=>copyMobile('#mobile-token','Код');$('#refresh-mobile-network').onclick=async e=>{const button=e.currentTarget;button.disabled=true;button.textContent='Проверяю…';try{await loadMobile()}finally{button.disabled=false;button.textContent='Обновить Wi‑Fi адрес'}};$('#regenerate-mobile-token').onclick=async()=>{if(!confirm('Сменить код? На уже подключённых телефонах понадобится ввести новый.'))return;try{await api('/api/mobile/token/regenerate',{method:'POST'});await loadMobile();$('#mobile-status').textContent='Код изменён. Введи новый код на телефоне.'}catch(e){$('#mobile-status').textContent=e.message}};
let adultPhotoPoll=0;
async function loadVideo(){const el=$('#video-folder-status');try{const v=await api('/api/video');const count=(v.files||[]).length;el.textContent=count?`В папке ${count} видео`:(v.ffmpeg_ready?'Готова принять исходники':'Видеодвижок установится через INSTALL EIRVEN AI.cmd')}catch(e){el.textContent=e.message}}
$('#open-video-folder').onclick=async()=>{const el=$('#video-folder-status');el.textContent='Открываю…';try{const r=await api('/api/video/open',{method:'POST'});el.textContent=r.opened?'Папка video открыта':`Папка: ${r.path}`;setTimeout(loadVideo,500)}catch(e){el.textContent=e.message}};
$('#voice-output-volume').addEventListener('input',e=>$('#voice-output-volume-value').textContent=`${e.target.value}%`);$('#voice-output-volume').addEventListener('change',e=>savePreferences({voice_output_volume:Number(e.target.value)/100}));$('#ui-language').addEventListener('change',e=>savePreferences({language:e.target.value}));$('#autostart-enabled').addEventListener('change',e=>savePreferences({autostart:e.target.checked}));$('#notifications-enabled').addEventListener('change',e=>savePreferences({notifications_enabled:e.target.checked}));$('#mini-mode-enabled').addEventListener('change',e=>savePreferences({mini_mode:e.target.checked}));$('#sphere-motion').addEventListener('change',e=>savePreferences({sphere_motion:e.target.checked}));$('#sphere-intensity').addEventListener('change',e=>savePreferences({sphere_intensity:e.target.value}));$('#desktop-comments-enabled').addEventListener('change',e=>savePreferences({desktop_comments_enabled:e.target.checked}));if($('#proactive-output-mode'))$('#proactive-output-mode').addEventListener('change',e=>savePreferences({proactive_output_mode:e.target.value}));if($('#confirmation-mode'))$('#confirmation-mode').addEventListener('change',e=>savePreferences({confirmation_mode:e.target.value}));$('#update-channel').addEventListener('change',e=>savePreferences({update_channel:e.target.value}));$('#auto-update-enabled').addEventListener('change',e=>savePreferences({auto_update_enabled:e.target.checked}));
async function setMicrophone(enabled){const button=$('#home-mic-toggle');if(button)button.classList.add('busy');try{const p=await savePreferences({voice_wake_enabled:!!enabled});state.preferences=p;if($('#voice-wake-enabled'))$('#voice-wake-enabled').checked=!!enabled;updateMicUI();await pollRuntime()}finally{if(button)button.classList.remove('busy')}}if($('#home-mic-toggle'))$('#home-mic-toggle').onclick=()=>setMicrophone(!micEnabled());$('#voice-wake-enabled').addEventListener('change',async e=>{try{await setMicrophone(e.target.checked)}catch{e.target.checked=!e.target.checked}});$('#desktop-control-enabled').addEventListener('change',async e=>{try{const r=await api('/api/settings/desktop-control',{method:'PUT',body:JSON.stringify({value:e.target.checked})});e.target.checked=!!r.enabled;if(state.preferences)state.preferences.desktop_control_enabled=!!r.enabled}catch(err){e.target.checked=!e.target.checked;addMessage('assistant',`Разрешение: ${err.message}`)}});$('#refresh-action-journal').onclick=()=>loadActionJournal();$('#clear-action-journal').onclick=async()=>{if(!confirm('Очистить локальный журнал действий?'))return;await api('/api/activity-journal',{method:'DELETE'});await loadActionJournal()};$('#mail-edit').onclick=()=>setMailSetup(!state.mailSetupOpen);$('#mail-auto-fill').onclick=fillMailProvider;$('#mail-explain').onclick=()=>{setView('home');$('#message-input').value='Помоги настроить почту и объясни каждое поле';autosize();sendMessage()};$('#mail-connect').onclick=async e=>{const b=e.currentTarget,before=b.textContent;b.disabled=true;b.textContent='Проверяю…';try{if(!$('#mail-imap-host').value.trim()||!$('#mail-smtp-host').value.trim())fillMailProvider();const payload={email:$('#mail-email').value.trim(),password:$('#mail-password').value,imap_host:$('#mail-imap-host').value.trim(),imap_port:Number($('#mail-imap-port').value||993),smtp_host:$('#mail-smtp-host').value.trim(),smtp_port:Number($('#mail-smtp-port').value||587),imap_ssl:$('#mail-imap-ssl').checked,smtp_starttls:$('#mail-smtp-starttls').checked,auto_move_obvious_spam:$('#mail-auto-spam').checked};await api('/api/mail/config',{method:'PUT',body:JSON.stringify(payload)});$('#mail-password').value='';state.mailSetupOpen=false;await loadMail()}catch(err){$('#mail-status').textContent=`Не подключено: ${err.message}`}finally{b.disabled=false;b.textContent=before}};$('#mail-disconnect').onclick=async()=>{if(!confirm('Удалить почтовое подключение и сохранённый пароль с этого компьютера?'))return;await api('/api/mail/config',{method:'DELETE'});$('#mail-password').value='';state.mailSetupOpen=false;await loadMail()};
async function checkUpdates(showErrors=true){const el=$('#update-status'),button=$('#install-update'),delivery=$('#delivery-channel');el.textContent='Проверяю…';button.hidden=true;try{const r=await api('/api/updates/check');const dc=String(r.delivery_channel||r.distribution?.claimed_channel||'standard').toUpperCase(),degraded=!!r.delivery_degraded;delivery.textContent=degraded?'EXPRESS → STANDARD':dc;if(r.ok){if(r.update_available){if(r.training_deferred){el.textContent=`Доступно ${r.latest} · установлю после дообучения`;button.hidden=true}else if(state.preferences?.auto_update_enabled){el.textContent=`Доступно ${r.latest} · автообновление включено`;button.hidden=true}else{el.textContent=`Доступно обновление ${r.latest}${degraded?' · временно стандартный канал':''}`;button.hidden=false}}else{el.textContent=`Установлена актуальная версия ${r.current}${degraded?' · Express временно недоступен':''}`}}else{el.textContent=r.error?`Проверка недоступна: ${r.error}`:'Не удалось проверить'}return r}catch(e){el.textContent='Не удалось проверить';if(showErrors)addMessage('assistant',`Обновления: ${e.message}`)}}
async function installUpdate(){const button=$('#install-update'),el=$('#update-status');button.disabled=true;el.textContent='Подготавливаю обновление…';try{const r=await api('/api/updates/install',{method:'POST'});if(r.deferred){el.textContent='Дообучение активно — обновлю после его завершения';return}el.textContent='Обновление скачивается в фоне. Эрви перезапустится после проверки файла.';button.hidden=true;pollUpdateStatus()}catch(e){el.textContent=`Не удалось обновить: ${e.message}`}finally{button.disabled=false}}
async function pollUpdateStatus(){try{const r=await api('/api/updates/status');if(r.state==='downloading'){$('#update-status').textContent=`${r.message||'Скачиваю'}${r.progress?` · ${Math.round(r.progress*100)}%`:''}`;setTimeout(pollUpdateStatus,900)}else if(['staging','applying'].includes(r.state)){$('#update-status').textContent=r.message||'Применяю обновление';setTimeout(pollUpdateStatus,900)}else if(r.state==='error'){$('#update-status').textContent=`Ошибка обновления: ${r.error||''}`}else if(r.message)$('#update-status').textContent=r.message}catch{}}
$('#check-updates').onclick=()=>checkUpdates(true);
$('#install-update').onclick=()=>installUpdate();
$('#shutdown-eirven').onclick=async()=>{if(!confirm('Отключить EIRVEN сейчас?'))return;const b=$('#shutdown-eirven');b.disabled=true;b.textContent='Выключаю…';document.body.classList.add('eirven-shutting-down');try{await api('/api/system/shutdown',{method:'POST'});addMessage('assistant','До встречи. Я выключаюсь.');const started=Date.now();const timer=setInterval(async()=>{try{await api('/api/ping');if(Date.now()-started>4500){clearInterval(timer);b.disabled=false;b.textContent='Повторить отключение';document.body.classList.remove('eirven-shutting-down')}}catch{clearInterval(timer);b.textContent='Отключено';setTimeout(()=>{try{window.close()}catch{}},350)}},250)}catch(e){b.disabled=false;b.textContent='Отключить';document.body.classList.remove('eirven-shutting-down');addMessage('assistant',`Не получилось отключиться: ${e.message}`)}};
$('#clear-current-chat').onclick=async()=>{const id=state.conversationId;if(id){try{await api(`/api/conversations/${id}`,{method:'DELETE'})}catch{}}state.conversationId=null;localStorage.removeItem('eirven_conversation');$('#messages').innerHTML='';await ensureConversation(true);addMessage('assistant','Диалог очищен.');};
$('#wipe-all-data').onclick=async()=>{const warning='Это необратимо удалит всю память EIRVEN, диалоги, заметки, календарь, привязки и журналы. Продолжить?';if(!confirm(warning))return;const phrase=prompt('Для подтверждения введи точно:\nУДАЛИТЬ ВСЕ ДАННЫЕ');if(phrase!=='УДАЛИТЬ ВСЕ ДАННЫЕ'){if(phrase!==null)alert('Фраза не совпала. Ничего не удалено.');return}const b=$('#wipe-all-data');b.disabled=true;b.textContent='Очищаю…';try{await api('/api/privacy/wipe',{method:'POST',body:JSON.stringify({confirmation:phrase,preserve_entitlement:true})});localStorage.clear();$('#messages').innerHTML='';addMessage('assistant','Все личные данные удалены. Запусти EIRVEN снова — знакомство начнётся с чистого листа.');b.textContent='Данные удалены'}catch(e){b.disabled=false;b.textContent='Забыть меня и очистить все данные';alert(`Очистка не выполнена: ${e.message}`)}};
function telegramMessage(text,bad=false){const el=$('#telegram-status');if(!el)return;el.textContent=String(text||'');el.classList.toggle('error',!!bad)}
function setTelegramSetup(open){state.telegramSetupOpen=!!open;const panel=$('#telegram-setup-panel');if(panel)panel.hidden=!state.telegramSetupOpen;const cfg=state.telegram?.config||{},button=$('#telegram-setup-toggle');if(button)button.textContent=state.telegramSetupOpen?'Скрыть настройку':(cfg.authorized?'Изменить подключение':cfg.configured?'Продолжить подключение':'Подключить Telegram')}
async function loadTelegram(){try{const t=await api('/api/telegram');state.telegram=t;const cfg=t.config||{},monitor=t.monitor||{};const loginBlock=$('#telegram-login-block');if(loginBlock)loginBlock.hidden=!!cfg.authorized;const ctl=$('#telegram-controls'),ctlHint=$('#telegram-connect-hint');if(ctl)ctl.hidden=!cfg.authorized;if(ctlHint)ctlHint.hidden=!!cfg.authorized;renderTelegramStyles(t);const auto=$('#telegram-autostart-monitor'),autoHint=$('#telegram-autostart-hint');if(auto){auto.disabled=!cfg.authorized;auto.checked=!!t.autostart_monitor;if(autoHint)autoHint.textContent=cfg.authorized?'Автоответы начнут работать сразу при запуске Эрви, без ручного включения.':'Сначала войди в Telegram — без активной сессии мониторинг запустить нельзя.';}$('#telegram-api-id').value=cfg.api_id||'';$('#telegram-phone').value=cfg.phone||'';$('#telegram-api-hash').placeholder=cfg.api_hash_masked?`Уже сохранён и защищён: ${cfg.api_hash_masked}`:'Вставь API Hash';$('#telegram-exclude-groups').checked=!!monitor.exclude_groups;$('#telegram-exclude-channels').checked=monitor.exclude_channels!==false;$('#telegram-max-per-hour').value=monitor.max_per_hour||20;const summary=$('#telegram-connection-summary');summary.textContent=cfg.authorized?`Telegram подключён для ${cfg.phone||'этого аккаунта'}. API ID, Hash и номер сохранены через ${cfg.secret_storage||'защищённое хранилище'}; повторный ввод не нужен.`:cfg.configured?'Данные сохранены и защищены. Осталось один раз получить код Telegram и подтвердить вход.':'Telegram не подключён. Это необязательно — остальные функции Эрви работают без него.';telegramMessage(t.status?.message||'Остановлено');setTelegramSetup(state.telegramSetupOpen);return t}catch(e){telegramMessage(e.message,true)}}
$('#telegram-setup-toggle').onclick=()=>setTelegramSetup(!state.telegramSetupOpen);$('#telegram-save-config').onclick=async()=>{try{const hash=$('#telegram-api-hash').value.trim();if(!hash&&!state.telegram?.config?.configured)throw new Error('При первом подключении нужен API Hash. После сохранения вводить его повторно не придётся.');await api('/api/telegram/config',{method:'PUT',body:JSON.stringify({api_id:Number($('#telegram-api-id').value),api_hash:hash,phone:$('#telegram-phone').value.trim()})});$('#telegram-api-hash').value='';telegramMessage('Данные сохранены и защищены. Теперь нажми «Получить код».');await loadTelegram()}catch(e){telegramMessage(e.message,true)}};
$('#telegram-request-code').onclick=async()=>{try{const r=await api('/api/telegram/login/code',{method:'POST'});telegramMessage(r.authorized?`Telegram уже подключён для ${r.username||r.user_id||'этого аккаунта'} — код не нужен, сессия активна. Мониторинг и ответы работают.`:r.message||'Код отправлен в Telegram — перенеси его в поле «Код входа»')}catch(e){telegramMessage(e.message,true)}};
$('#telegram-confirm-login').onclick=async()=>{try{const r=await api('/api/telegram/login/confirm',{method:'POST',body:JSON.stringify({code:$('#telegram-login-code').value.trim(),password:$('#telegram-login-password').value})});$('#telegram-login-password').value='';if(r.requires_password)telegramMessage('Telegram просит пароль двухэтапной защиты — введи его и подтверди вход ещё раз');else telegramMessage(r.message||'Вход подтверждён');await loadTelegram()}catch(e){telegramMessage(e.message,true)}};
$('#telegram-reply-unread').onclick=async()=>{const b=$('#telegram-reply-unread');b.disabled=true;try{const r=await api('/api/telegram/reply-unread',{method:'POST',body:JSON.stringify({exclude_groups:$('#telegram-exclude-groups').checked,exclude_channels:$('#telegram-exclude-channels').checked,max_per_hour:Number($('#telegram-max-per-hour').value||20)})});telegramMessage(r.message||`Ответов отправлено: ${r.replied||0}`,!r.ok)}catch(e){telegramMessage(e.message,true)}finally{b.disabled=false}};
$('#telegram-monitor-start').onclick=async()=>{const b=$('#telegram-monitor-start');b.disabled=true;try{const r=await api('/api/telegram/monitor',{method:'POST',body:JSON.stringify({exclude_groups:$('#telegram-exclude-groups').checked,exclude_channels:$('#telegram-exclude-channels').checked,max_per_hour:Number($('#telegram-max-per-hour').value||20)})});telegramMessage(r.status?.message||'Мониторинг запущен')}catch(e){telegramMessage(e.message,true)}finally{b.disabled=false}};
$('#telegram-monitor-stop').onclick=async()=>{try{const r=await api('/api/telegram/stop',{method:'POST'});telegramMessage(r.message||'Мониторинг остановлен')}catch(e){telegramMessage(e.message,true)}};
$('#telegram-custom-task-run').onclick=async()=>{const b=$('#telegram-custom-task-run'),task=$('#telegram-custom-task').value.trim();if(!task){telegramMessage('Сначала напиши задачу');return}b.disabled=true;telegramMessage('Выполняю через Telegram API…');try{const r=await api('/api/telegram/task',{method:'POST',body:JSON.stringify({task})});telegramMessage(r.message||'Задача выполнена через Telegram API',!r.ok)}catch(e){telegramMessage(e.message,true)}finally{b.disabled=false}};
$('#voice-preview-button').onclick=async e=>{const b=e.currentTarget,old=b.textContent;b.disabled=true;b.textContent='Говорю…';try{const res=await fetch('/api/voice/speak',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:'Привет. Я Эрви. Говорю спокойно и естественно, как в обычном разговоре.',emotion:'auto'})});if(!res.ok)throw new Error((await res.text())||'Не удалось получить голос');const blob=await res.blob(),audio=$('#voice-preview');audio.src=URL.createObjectURL(blob);await audio.play()}catch(err){addMessage('assistant',`Голос: ${err.message}`)}finally{b.textContent=old;b.disabled=false}};

$('#main-orb').onclick=()=>{if(state.runtime.onboarding_complete===false){openOnboarding();return}$('#message-input').focus()};

const ONBOARDING_DRAFT_KEY='eirven_onboarding_draft_r67';
function readOnboardingDraft(){try{const value=JSON.parse(localStorage.getItem(ONBOARDING_DRAFT_KEY)||'{}');return{style:['balanced','friendly','brief','free'].includes(value.style)?value.style:'balanced',user_address:String(value.user_address||'').slice(0,48),step:Math.max(1,Math.min(4,Number(value.step)||1))}}catch{return{style:'balanced',user_address:'',step:1}}}
let onboardingDraft=readOnboardingDraft();
function persistOnboardingDraft(){try{localStorage.setItem(ONBOARDING_DRAFT_KEY,JSON.stringify(onboardingDraft))}catch{}}
function showOnboardStep(n){n=Math.max(1,Math.min(4,Number(n)||1));onboardingDraft.step=n;persistOnboardingDraft();$$('.onboarding-step').forEach(s=>s.classList.toggle('active',Number(s.dataset.step)===n));$$('.onboarding-dots i').forEach((d,i)=>d.classList.toggle('active',i===n-1));const active=$(`.onboarding-step[data-step="${n}"]`);active?.querySelector('input,button')?.focus({preventScroll:true})}
function openOnboarding(){document.body.classList.add('onboarding-open');$('#app-shell').inert=true;$('#onboarding').setAttribute('aria-hidden','false');$('#onboard-user-name').value=onboardingDraft.user_address||state.identity?.user_address||'';setPresetButtons($('#onboard-style'),onboardingDraft.style);showOnboardStep(onboardingDraft.step)}
function closeOnboarding(){document.body.classList.remove('onboarding-open');$('#app-shell').inert=false;$('#onboarding').setAttribute('aria-hidden','true')}
$('#onboard-user-name').addEventListener('input',e=>{onboardingDraft.user_address=e.target.value.slice(0,48);persistOnboardingDraft()});
$$('.onboard-next').forEach(b=>b.onclick=()=>{const next=Number(b.dataset.next);if(next===2){onboardingDraft.style=onboardingDraft.style||presetFromStyle(state.style)||'balanced';setPresetButtons($('#onboard-style'),onboardingDraft.style)}if(next===3){const name=$('#onboard-user-name').value.trim();if(!name){$('#onboard-user-name').focus();return}onboardingDraft.user_address=name;persistOnboardingDraft();$('#onboard-greeting').textContent=`Готова, ${name}.` }showOnboardStep(next)});
$('#finish-onboarding').onclick=async e=>{const b=e.currentTarget;b.disabled=true;b.textContent='Сохраняю…';try{const userAddress=(onboardingDraft.user_address||$('#onboard-user-name').value).trim();if(!userAddress){showOnboardStep(2);$('#onboard-user-name').focus();return}const styleName=onboardingDraft.style||'balanced',result=await api('/api/onboarding/complete',{method:'POST',body:JSON.stringify({user_address:userAddress,style:{...(STYLE_PRESETS[styleName]||STYLE_PRESETS.balanced)},voice_wake_enabled:$('#onboard-voice-wake').checked,desktop_control_enabled:$('#onboard-desktop-control').checked})});state.identity=result.identity;state.style=result.style;state.preferences=result.preferences;localStorage.removeItem(ONBOARDING_DRAFT_KEY);onboardingDraft={style:presetFromStyle(state.style),user_address:state.identity?.user_address||'',step:1};$('#user-address').value=state.identity?.user_address||'';setPresetButtons($('#communication-style'),onboardingDraft.style);applyAppearance(state.preferences||{});updateMicUI();closeOnboarding();$('#message-input').focus();pollRuntime()}catch(err){b.textContent='Попробовать ещё';const note=$('.onboarding-step.active p');if(note)note.textContent=`Не удалось сохранить знакомство: ${err.message}. Ничего не потеряно — можно повторить.`}finally{b.disabled=false;if(b.textContent==='Сохраняю…')b.textContent='Запустить'}};

// Compact choices are mirrored beside the sphere so clarification never hides in the chat rail.
function renderChoicePanel(node,route={}){const choices=Array.isArray(route.ui_choices)?route.ui_choices.slice(0,5):[];const old=node.nextElementSibling;if(old?.classList?.contains('choice-panel'))old.remove();const dock=$('#sphere-intervention');const shouldDock=choices.length||route.needs_user||route.ui_question;if(dock){dock.hidden=!shouldDock;dock.innerHTML='';if(route.ui_question){const q=document.createElement('small');q.textContent=route.ui_question;dock.append(q)}if(shouldDock){const row=document.createElement('div');row.className='choice-actions';choices.forEach(choice=>{const b=document.createElement('button');b.type='button';b.textContent=choice.label||choice.value||'Выбрать';b.onclick=()=>{dismissChoicePanels(true);$('#message-input').value=String(choice.value||choice.label||'');autosize();sendMessage()};row.append(b)});const manual=document.createElement('button');manual.type='button';manual.className='choice-manual';manual.textContent='Ввести свой вариант';manual.onclick=()=>{dismissChoicePanels(true);$('#message-input').focus()};row.append(manual);dock.append(row)}syncOrbLayout()}if(!choices.length)return;const panel=document.createElement('div');panel.className='choice-panel';if(route.ui_question&&String(route.ui_question).trim()&&String(route.ui_question).trim()!==String(node.textContent||'').trim()){const q=document.createElement('small');q.textContent=route.ui_question;panel.append(q)}const actions=document.createElement('div');actions.className='choice-actions';choices.forEach(choice=>{const b=document.createElement('button');b.type='button';b.textContent=choice.label||choice.value||'Выбрать';b.onclick=()=>{dismissChoicePanels(true);$('#message-input').value=String(choice.value||choice.label||'');autosize();sendMessage()};actions.append(b)});panel.append(actions);node.after(panel);$('#messages').scrollTop=$('#messages').scrollHeight}
function syncOrbLayout(){const wrap=$('.presence-wrap'),stage=$('#stage'),dock=$('#sphere-intervention');if(!wrap||!stage)return;let sx=0,sy=0;const w=wrap.getBoundingClientRect(),s=stage.getBoundingClientRect();if(dock&&!dock.hidden){const d=dock.getBoundingClientRect(),overlap=d.right>w.left&&d.left<w.right;if(overlap){sx=d.left<w.left?Math.min(150,w.left-d.right-16):Math.max(-150,w.right-d.left+16);sy=d.top<w.top?80:-80}}const minX=s.left+8-w.left,maxX=s.right-8-w.right;const minY=s.top+8-w.top,maxY=s.bottom-8-w.bottom;sx=Math.max(minX,Math.min(sx,maxX));sy=Math.max(minY,Math.min(sy,maxY));wrap.style.setProperty('--orb-shift-x',`${Math.round(sx)}px`);wrap.style.setProperty('--orb-shift-y',`${Math.round(sy)}px`)}
async function loadCamera(){const toggle=$('#camera-enabled'),copy=$('#camera-status-copy'),previewWrap=$('#camera-preview-wrap');if(!toggle)return;try{const c=await api('/api/camera');state.camera=c;const sphereOn=state.preferences?.mini_mode!==false;toggle.disabled=!sphereOn||c.available===false;toggle.checked=!!c.running;copy.textContent=!sphereOn?'Включи сферу в разделе «Общее».':c.available===false?(c.error||'Камера недоступна на этом компьютере.'):(c.gesture_available?'Выключена · жесты доступны':'Выключена · для жестов нужен MediaPipe');previewWrap.hidden=!c.running;if(c.running){$('#camera-preview').src='/api/camera/stream';}else $('#camera-preview').removeAttribute('src')}catch(e){toggle.disabled=true;copy.textContent=`Не удалось проверить камеру: ${e.message}`}}
async function setCamera(enabled){const toggle=$('#camera-enabled');if(!toggle)return;toggle.disabled=true;try{state.camera=await api(enabled?'/api/camera/start':'/api/camera/stop',{method:'POST'});toggle.checked=!!state.camera.running;await loadCamera()}catch(e){toggle.checked=false;$('#camera-status-copy').textContent=`Камера: ${e.message}`}finally{syncCameraAvailability()}}
function syncCameraAvailability(){const toggle=$('#camera-enabled');if(!toggle)return;const sphereOn=state.preferences?.mini_mode!==false;toggle.disabled=!sphereOn||state.camera?.available===false;if(!sphereOn&&toggle.checked)setCamera(false)}
$('#camera-enabled')?.addEventListener('change',e=>setCamera(e.target.checked));
$('#mini-mode-enabled').addEventListener('change',()=>setTimeout(syncCameraAvailability,50));
setInterval(async()=>{if(state.camera?.running){try{state.camera=await api('/api/camera');const g=state.camera.gesture||{};if(g.fist&&Number.isFinite(g.x)&&Number.isFinite(g.y)){const wrap=$('.presence-wrap');if(wrap){wrap.style.setProperty('--orb-shift-x',`${Math.round((.5-Number(g.x))*150)}px`);wrap.style.setProperty('--orb-shift-y',`${Math.round((Number(g.y)-.5)*110)}px`);syncOrbLayout()}}}catch{}}},250);
window.addEventListener('resize',syncOrbLayout);new ResizeObserver(syncOrbLayout).observe($('#stage')); 
(async function init(){try{await loadIdentity();await loadStyle();await loadPreferences();if(!onboardingDraft.user_address)onboardingDraft.user_address=state.identity?.user_address||'';if(!localStorage.getItem(ONBOARDING_DRAFT_KEY))onboardingDraft.style=presetFromStyle(state.style);if(!state.identity?.onboarding_completed)openOnboarding()}catch(e){openOnboarding();const note=$('.onboarding-step.active p');if(note)note.textContent=`Знакомство открылось, но часть локальных настроек пока недоступна: ${e.message}`}try{await ensureConversation();await loadHistory()}catch(e){$('#chat-model-status').textContent=`Чат запускается: ${e.message}`}})();

function renderTelegramStyles(t){
  const grid=$('#telegram-style-grid');
  if(!grid)return;
  const styles=Array.isArray(t.styles)?t.styles:[];
  const current=t.reply_style||'manager';
  grid.replaceChildren();
  styles.forEach(st=>{
    const card=document.createElement('button');
    card.type='button';
    card.className='style-card'+(st.key===current?' active':'');
    card.dataset.styleKey=st.key;
    const icon=document.createElement('i');icon.textContent=st.emoji||'💬';
    const box=document.createElement('span');
    const title=document.createElement('b');title.textContent=st.title||st.key;
    const hint=document.createElement('small');hint.textContent=st.hint||'';
    box.append(title,hint);
    card.append(icon,box);
    card.onclick=async()=>{
      try{
        await api('/api/telegram/prefs',{method:'POST',body:JSON.stringify({reply_style:st.key})});
        grid.querySelectorAll('.style-card').forEach(c=>c.classList.toggle('active',c===card));
        telegramMessage(`Стиль ответов: ${st.title}`);
      }catch(e){telegramMessage(e.message,true)}
    };
    grid.append(card);
  });
}

$('#telegram-autostart-monitor')?.addEventListener('change',async(e)=>{try{await api('/api/telegram/prefs',{method:'POST',body:JSON.stringify({autostart_monitor:e.target.checked})});telegramMessage(e.target.checked?'Мониторинг будет запускаться вместе с Эрви.':'Автозапуск мониторинга выключен.')}catch(err){telegramMessage(err.message,true);e.target.checked=!e.target.checked}});

async function refreshTelegramAutostart(){
  // The toggle lives in the General tab but its enabled state depends on Telegram
  // authorization, which is only fetched when the Telegram tab is opened. Without
  // this the switch stays disabled even after a successful login.
  const auto=$('#telegram-autostart-monitor'),hint=$('#telegram-autostart-hint');
  if(!auto)return;
  try{
    const t=await api('/api/telegram');
    const authorized=!!((t.config||{}).authorized);
    auto.disabled=!authorized;
    auto.checked=!!t.autostart_monitor;
    if(hint)hint.textContent=authorized
      ?'Автоответы начнут работать сразу при запуске Эрви, без ручного включения.'
      :'Сначала войди в Telegram — без активной сессии мониторинг запустить нельзя.';
  }catch{}
}

/* r68 · выбор чатов, стили по чатам, рассылка, обучение */
(function(){
  const picker = document.querySelector('[data-chat-picker]');
  if (!picker) return;
  const listBox = picker.querySelector('[data-picker-list]');
  const countBox = picker.querySelector('[data-picker-count]');
  const titleBox = picker.querySelector('[data-picker-title]');
  const hintBox = picker.querySelector('[data-picker-hint]');
  const search = picker.querySelector('#chat-picker-search');
  let chats = [];
  let mode = 'exclude';           // 'exclude' | 'broadcast'
  let selected = new Set();
  let styles = {};
  let styleCatalogue = [];
  let broadcastTargets = [];

  function close(){ picker.hidden = true; }
  picker.querySelector('[data-picker-close]')?.addEventListener('click', close);
  picker.addEventListener('click', (e) => { if (e.target === picker) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !picker.hidden) close(); });

  function render(){
    const q = (search?.value || '').trim().toLowerCase();
    const rows = chats.filter(c => !q || (c.title || '').toLowerCase().includes(q));
    listBox.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement('p');
      empty.className = 'setting-copy';
      empty.textContent = chats.length ? 'Ничего не найдено.' : 'Чаты не загрузились. Проверь подключение Telegram.';
      listBox.append(empty);
      return;
    }
    rows.forEach(chat => {
      const row = document.createElement('div');
      row.className = 'chat-row' + (selected.has(chat.id) ? ' picked' : '');

      const avatar = document.createElement('span');
      avatar.className = 'chat-avatar';
      if (chat.avatar) {
        const img = document.createElement('img');
        img.src = chat.avatar; img.alt = '';
        avatar.append(img);
      } else {
        avatar.textContent = (chat.title || '?').trim().charAt(0).toUpperCase();
      }

      const info = document.createElement('span');
      info.className = 'chat-info';
      const name = document.createElement('b');
      name.textContent = chat.title || chat.id;
      const meta = document.createElement('small');
      meta.textContent = (chat.kind === 'group' ? 'группа' : chat.kind === 'channel' ? 'канал' : 'личный чат')
        + (chat.unread ? ` · ${chat.unread} непрочитанных` : '');
      info.append(name, meta);

      const right = document.createElement('span');
      right.className = 'chat-right';

      if (mode === 'exclude') {
        const styleSel = document.createElement('select');
        styleSel.className = 'chat-style';
        const def = document.createElement('option');
        def.value = ''; def.textContent = 'Общий стиль';
        styleSel.append(def);
        styleCatalogue.forEach(st => {
          const opt = document.createElement('option');
          opt.value = st.key; opt.textContent = `${st.emoji || ''} ${st.title}`.trim();
          styleSel.append(opt);
        });
        styleSel.value = styles[chat.id] || '';
        styleSel.onclick = (e) => e.stopPropagation();
        styleSel.onchange = () => {
          if (styleSel.value) styles[chat.id] = styleSel.value; else delete styles[chat.id];
        };
        right.append(styleSel);
      }

      const mark = document.createElement('i');
      mark.className = 'chat-mark';
      mark.textContent = selected.has(chat.id) ? '✕' : '';
      right.append(mark);

      row.append(avatar, info, right);
      row.onclick = () => {
        if (selected.has(chat.id)) selected.delete(chat.id); else selected.add(chat.id);
        render();
      };
      listBox.append(row);
    });
    if (countBox) {
      countBox.textContent = mode === 'exclude'
        ? `Не отвечать: ${selected.size}`
        : `Выбрано: ${selected.size}`;
    }
  }
  search?.addEventListener('input', render);

  async function open(nextMode){
    mode = nextMode;
    picker.hidden = false;
    if (titleBox) titleBox.textContent = mode === 'exclude' ? 'Выбрать исключения' : 'Выбрать чаты для рассылки';
    if (hintBox) hintBox.textContent = mode === 'exclude'
      ? 'Отмеченным чатам Эрви отвечать не будет. Стиль можно задать отдельно для каждого.'
      : 'Отметь, кому отправить сообщение.';
    listBox.replaceChildren(Object.assign(document.createElement('p'), {className:'setting-copy', textContent:'Загружаю чаты…'}));
    try {
      // Каталог стилей уже загружен вкладкой Telegram — берём его из состояния,
      // чтобы не делать второй запрос к тем же данным одновременно с первым.
      const cached = (state.telegram && Array.isArray(state.telegram.styles)) ? state.telegram.styles : null;
      const data = await api('/api/telegram/chats?limit=40');
      chats = Array.isArray(data.chats) ? data.chats : [];
      styleCatalogue = cached || [];
      if (!styleCatalogue.length) {
        try {
          const prefs = await api('/api/telegram');
          styleCatalogue = Array.isArray(prefs.styles) ? prefs.styles : [];
        } catch { styleCatalogue = []; }
      }
      styles = {};
      chats.forEach(c => { if (c.style) styles[c.id] = c.style; });
      selected = new Set(mode === 'exclude'
        ? chats.filter(c => c.excluded).map(c => c.id)
        : broadcastTargets);
      render();
    } catch (e) {
      listBox.replaceChildren(Object.assign(document.createElement('p'), {className:'form-error', textContent:e.message}));
    }
  }

  picker.querySelector('[data-picker-save]')?.addEventListener('click', async () => {
    if (mode === 'broadcast') {
      broadcastTargets = Array.from(selected);
      const label = document.querySelector('#telegram-broadcast-count');
      if (label) label.textContent = broadcastTargets.length ? `Выбрано чатов: ${broadcastTargets.length}` : 'Не выбрано';
      close();
      return;
    }
    try {
      await api('/api/telegram/chat-prefs', {
        method: 'POST',
        body: JSON.stringify({ excluded: Array.from(selected), styles })
      });
      telegramMessage(`Сохранено. Не отвечать: ${selected.size} чат(ов).`);
      close();
    } catch (e) { telegramMessage(e.message, true); }
  });

  document.querySelector('#telegram-pick-chats')?.addEventListener('click', () => open('exclude'));
  document.querySelector('#telegram-broadcast-pick')?.addEventListener('click', () => open('broadcast'));

  document.querySelector('#telegram-broadcast-send')?.addEventListener('click', async () => {
    const text = (document.querySelector('#telegram-broadcast-text')?.value || '').trim();
    const when = document.querySelector('#telegram-broadcast-when')?.value || '';
    if (!broadcastTargets.length) { telegramMessage('Сначала выбери чаты.', true); return; }
    if (text.length < 2) { telegramMessage('Напиши текст сообщения.', true); return; }
    const btn = document.querySelector('#telegram-broadcast-send');
    if (btn) btn.disabled = true;
    try {
      const r = await api('/api/telegram/broadcast', {
        method: 'POST',
        body: JSON.stringify({ chats: broadcastTargets, message: text, when: when || null })
      });
      telegramMessage(r.message || 'Готово', !r.ok);
    } catch (e) { telegramMessage(e.message, true); }
    finally { if (btn) btn.disabled = false; }
  });
})();


/* r70 · перенос данных на другой компьютер.
   Раньше это было только скриптом в консоли — человек его не находил. */
(function(){
  const hint = document.querySelector('#transfer-hint');
  const setHint = (text, bad) => {
    if (!hint) return;
    hint.textContent = text;
    hint.style.color = bad ? '#ff9aa8' : '';
  };

  document.querySelector('#transfer-export')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const suggested = 'C:\\Users\\' + (window.eirvenUser || '') + '\\Desktop\\eirven-backup.zip';
    const path = window.prompt('Куда сохранить файл переноса?', suggested.includes('\\\\') ? 'D:\\eirven-backup.zip' : 'D:\\eirven-backup.zip');
    if (!path) return;
    btn.disabled = true;
    setHint('Выгружаю данные… это может занять минуту.');
    try {
      const r = await api('/api/transfer/export', { method:'POST', body: JSON.stringify({ path }) });
      setHint(r.message || 'Готово.');
    } catch (err) {
      setHint(err.message, true);
    } finally { btn.disabled = false; }
  });

  document.querySelector('#transfer-import')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const path = window.prompt('Путь к файлу выгрузки (снятому на прежнем компьютере):', 'D:\\eirven-backup.zip');
    if (!path) return;
    if (!window.confirm('Текущие переписка и настройки будут заменены. Копия текущих данных сохранится рядом. Продолжить?')) return;
    btn.disabled = true;
    setHint('Восстанавливаю данные…');
    try {
      const r = await api('/api/transfer/import', { method:'POST', body: JSON.stringify({ path }) });
      setHint(r.message || 'Данные восстановлены.');
    } catch (err) {
      setHint(err.message, true);
    } finally { btn.disabled = false; }
  });
})();

 document.querySelector('#telegram-save-prompt')?.addEventListener('click',async()=>{try{await api('/api/telegram/general-prompt',{method:'PUT',body:JSON.stringify({general_prompt:document.querySelector('#telegram-general-prompt').value})});telegramMessage('Общий промпт сохранён')}catch(e){telegramMessage(e.message,true)}}); document.querySelector('#delete-eirven')?.addEventListener('click',()=>{const c=prompt('Вы уверены, что хотите удалить Эрви? Введите: Отменить, Удалить только Эрви или Удалить всё');if(c==='Удалить всё')document.querySelector('#wipe-all-data')?.click();else if(c==='Удалить только Эрви')alert('Компоненты можно удалить деинсталлятором Windows; личные данные сохранены.');});
