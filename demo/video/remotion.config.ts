import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setJpegQuality(95);

// The terminal text is imported verbatim from src/captures/*.txt, which
// capture/capture.py writes from real runs of the tool. asset/source turns
// each file into its exact string contents at build time.
Config.overrideWebpackConfig((config) => ({
  ...config,
  module: {
    ...config.module,
    rules: [...(config.module?.rules ?? []), {test: /\.txt$/, type: 'asset/source'}],
  },
}));
