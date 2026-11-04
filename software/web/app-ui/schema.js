import {esc,title,$$} from '../core.js';
let fieldSequence=0;

// Small, dependency-free editor for the platform's bounded JSON schema subset.
// String lists use one entry per line; structured arrays/maps use JSON.
export function schemaFields(schema={},values={},prefix=[],depth=0){
  const properties=schema.properties||{},required=new Set(schema.required||[]);
  const entries=Object.entries(properties).slice(0,64).sort((a,b)=>(a[1]['x-order']??1000)-(b[1]['x-order']??1000));
  if(depth===0&&entries.some(([name,spec])=>spec['x-advanced']&&!required.has(name))){
    const basic=Object.fromEntries(entries.filter(([name,spec])=>!spec['x-advanced']||required.has(name)));
    const advanced=Object.fromEntries(entries.filter(([name,spec])=>spec['x-advanced']&&!required.has(name)));
    return schemaFields({...schema,properties:basic},values,prefix,depth+1)+`<details class="full app-advanced-fields"><summary>Advanced options</summary><div class="details-body"><div class="form-grid">${schemaFields({...schema,properties:advanced},values,prefix,depth+1)}</div></div></details>`;
  }
  return entries.map(([name,spec])=>{
    const path=[...prefix,name],value=values?.[name]??spec.default;
    const label=spec.title||title(name),hint=spec.description||'';
    const hintId=`app-field-help-${++fieldSequence}`;
    const type=Array.isArray(spec.type)?spec.type.find(t=>t!=='null'):spec.type;
    const attr=`aria-label="${esc(label)}" ${hint?`aria-describedby="${hintId}"`:''} data-schema-path="${esc(JSON.stringify(path))}" data-schema-type="${esc(type||'string')}" ${required.has(name)&&type!=='boolean'?'required':''} ${spec.readOnly?'disabled':''}`;
    if(type==='object'&&spec.properties&&depth<4){
      return `<fieldset class="full app-schema-group"><legend>${esc(label)}</legend>${hint?`<p class="hint">${esc(hint)}</p>`:''}<div class="form-grid">${schemaFields(spec,value||{},path,depth+1)}</div></fieldset>`;
    }
    let control;
    if(spec.enum){control=`<select ${attr} data-schema-enum="1">${!required.has(name)&&value===undefined?'<option value="">Choose…</option>':''}${spec.enum.map(v=>`<option value="${esc(JSON.stringify(v))}" ${JSON.stringify(v)===JSON.stringify(value)?'selected':''}>${esc(v)}</option>`).join('')}</select>`}
    else if(type==='boolean'){control=`<input type="checkbox" ${attr} ${value?'checked':''}>`;return `<label class="check app-schema-check">${control}<span>${esc(label)}${hint?`<small id="${hintId}">${esc(hint)}</small>`:''}</span></label>`}
    else if(type==='array'&&spec.items?.type==='string'){control=`<textarea ${attr} data-schema-lines="1" placeholder="One item per line">${esc(Array.isArray(value)?value.join('\n'):'')}</textarea>`}
    else if(type==='object'||type==='array'){control=`<textarea ${attr} data-schema-json="1" spellcheck="false" placeholder="${type==='array'?'[]':'{}'}">${esc(value===undefined?'':JSON.stringify(value,null,2))}</textarea>`}
    else if(type==='integer'||type==='number'){control=`<input type="number" ${attr} value="${esc(value??'')}" step="${type==='integer'?'1':'any'}" ${spec.minimum!==undefined?`min="${esc(spec.minimum)}"`:''} ${spec.maximum!==undefined?`max="${esc(spec.maximum)}"`:''}>`}
    else if(spec.format==='textarea'||name==='prompt'||(spec.maxLength||0)>2000){control=`<textarea ${attr} ${spec.maxLength?`maxlength="${esc(spec.maxLength)}"`:''}>${esc(value??'')}</textarea>`}
    else{control=`<input type="${spec.format==='uri'?'url':'text'}" ${attr} value="${esc(value??'')}" ${spec.maxLength?`maxlength="${esc(spec.maxLength)}"`:''} ${spec.minLength?`minlength="${esc(spec.minLength)}"`:''}>`}
    return `<label class="${type==='object'||type==='array'||name==='prompt'?'full':''}">${esc(label)}${required.has(name)?'<span class="sr-only"> (required)</span>':''}${control}${hint?`<small id="${hintId}">${esc(hint)}</small>`:''}</label>`;
  }).join('');
}

export function schemaValues(form,{omitEmpty=true,initial={}}={}){
  const result=JSON.parse(JSON.stringify(initial));
  for(const input of $$('[data-schema-path]',form)){
    if(input.disabled)continue;
    const path=JSON.parse(input.dataset.schemaPath),type=input.dataset.schemaType;
    if(path.some(key=>['__proto__','constructor','prototype'].includes(key)))throw new Error('Unsupported field name.');
    let value=input.value;
    if(input.type==='checkbox')value=input.checked;
    else if(value===''){
      if(omitEmpty)continue;
      if(input.dataset.schemaEnum||['integer','number','object','array'].includes(type))value=null;
    }else if(input.dataset.schemaLines){
      value=value.split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
    }else if(input.dataset.schemaJson||input.dataset.schemaEnum){
      try{value=JSON.parse(value)}catch{throw new Error(`${title(path.at(-1))} must contain valid JSON.`)}
    }else if(type==='integer'||type==='number'){
      value=Number(value);
      if(!Number.isFinite(value)||(type==='integer'&&!Number.isInteger(value)))throw new Error(`${title(path.at(-1))} must be ${type==='integer'?'a whole number':'a number'}.`);
    }
    let target=result;
    for(const key of path.slice(0,-1)){if(!target[key]||typeof target[key]!=='object'||Array.isArray(target[key]))target[key]={};target=target[key]}
    target[path.at(-1)]=value;
  }
  return result;
}
