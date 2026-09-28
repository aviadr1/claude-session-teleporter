// How long ago each fixture session was last active, as the stylised app
// shows it. These match make_fixture.py (age_h = 1, 3 and 5 hours); the
// titles themselves come from the captures.
import {personalTitleBefore, workTitles} from './captures';

export const AGE: Record<string, string> = {
  [workTitles[0]]: '1 hour ago',
  [workTitles[1]]: '5 hours ago',
  [personalTitleBefore]: '3 hours ago',
};
