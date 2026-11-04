// Presentation hints are separate from tool validation and app execution.
// Hidden fields retain their schema defaults; the server remains authoritative.
const forms={
  'decision-helper':{
    decide:{field:'action',actions:{
      coin:{fields:[],title:'Flip a coin',description:'Heads or tails? A little nudge for your next decision.',button:'Flip a coin'},
      dice:{fields:['count','sides'],title:'Roll the dice',description:'Choose your dice, give them a roll, and see where they land.',button:'Roll the dice'},
      choose:{fields:['choices'],title:'Pick a little possibility',description:'Add your options, one per line. Robbi will choose one at random.',button:'Choose for me'},
      shuffle:{fields:['choices'],title:'Mix things up',description:'Add your options, one per line, and give them a fresh random order.',button:'Shuffle my options'}
    }}
  }
};
const defaults=new WeakMap();

export function applyFormGuidance(form,{appId,tool}={}){
  if(!form)return null;
  const guide=forms[appId]?.[tool?.name];
  if(!guide){defaults.delete(form);return null}
  const controls=[...form.querySelectorAll('[data-schema-path]')];
  const controller=controls.find(input=>input.dataset.schemaPath===JSON.stringify([guide.field]));
  let action=controller?.value;
  if(controller?.dataset.schemaEnum)try{action=JSON.parse(action)}catch{}
  const presentation=guide.actions[action];if(!presentation)return null;
  const hiddenDefaults={};
  for(const input of controls){
    let path;try{path=JSON.parse(input.dataset.schemaPath)}catch{continue}
    if(path.length!==1||path[0]===guide.field)continue;
    const spec=tool.input_schema?.properties?.[path[0]]||{},hidden=!presentation.fields.includes(path[0]);
    const label=input.closest('label');if(label)label.hidden=hidden;
    input.disabled=hidden||!!spec.readOnly;
    if(hidden&&spec.default!==undefined)hiddenDefaults[path[0]]=spec.default;
  }
  defaults.set(form,hiddenDefaults);
  return presentation;
}

export function formGuidanceDefaults(form){return {...defaults.get(form)}}
