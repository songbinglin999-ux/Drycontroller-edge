import preprocess from "svelte-preprocess";

/** @type {import('svelte').Config} */
const config = {
  preprocess: preprocess(),
  kit: {
    alias: {
      $lib: "src/lib"
    }
  }
};

export default config;

