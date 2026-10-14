/* Photo Boss galleries section — uses existing authenticated delivery controls.
 * All mutations and authorization stay in the established Mini App API.
 */
import {loadShootControl,shootControlView} from './delivery.js';

export async function loadGalleries(){return loadShootControl();}
export function galleriesView(){
  return '<section class="page-heading"><h1>Клиентские галереи</h1><p>Галереи связаны с записями и съёмками Photo Boss. Управляйте передачей фотографий клиентам через существующий защищённый модуль.</p></section>'+shootControlView();
}
