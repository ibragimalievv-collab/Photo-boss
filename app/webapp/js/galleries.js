/* Photo Boss galleries section — uses existing authenticated delivery controls.
 * All mutations and authorization stay in the established Mini App API.
 */
import {loadShootControl,shootControlView} from './delivery.js';

export async function loadGalleries(){return loadShootControl();}
export function galleriesView(){
  return shootControlView({title:'Клиентские галереи',description:'Откройте съёмку, загрузите фотографии и настройте доступ гостя.'});
}
