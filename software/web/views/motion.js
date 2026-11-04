import {$,heading} from '../core.js';
import {mountManualDrive} from '../manual-drive.js';

export async function motion(root,life){
  root.innerHTML=heading('MANUAL CONTROL','Drive Robbi.','Choose a direction, set a distance or angle, and move once.')+`<div id="manual-panel"></div><p class="section-gap hint">Looking for mapping and navigation? Open <a href="#map">Map & explore</a>.</p>`;
  mountManualDrive($('#manual-panel'),life);
}
