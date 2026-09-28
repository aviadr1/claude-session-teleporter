import {loadFont} from '@remotion/fonts';
import monoRegular from 'jetbrains-mono/fonts/webfonts/JetBrainsMonoNL-Regular.woff2';
import monoBold from 'jetbrains-mono/fonts/webfonts/JetBrainsMonoNL-Bold.woff2';
import sans400 from '@fontsource/inter/files/inter-latin-400-normal.woff2';
import sans600 from '@fontsource/inter/files/inter-latin-600-normal.woff2';
import sans800 from '@fontsource/inter/files/inter-latin-800-normal.woff2';
import sans800Italic from '@fontsource/inter/files/inter-latin-800-italic.woff2';

// The full JetBrains Mono build, not a Google Fonts subset: the tool's output
// uses box-drawing and symbol glyphs (─ │ ═ ▶ ░ █ ● ✓ ✗) that the subsets
// leave out, and a fallback font would break the column alignment.
export const MONO = 'JetBrains Mono NL';
export const SANS = 'Inter';

loadFont({family: MONO, url: monoRegular, weight: '400'});
loadFont({family: MONO, url: monoBold, weight: '700'});
loadFont({family: SANS, url: sans400, weight: '400'});
loadFont({family: SANS, url: sans600, weight: '600'});
loadFont({family: SANS, url: sans800, weight: '800'});
loadFont({family: SANS, url: sans800Italic, weight: '800', style: 'italic'});
