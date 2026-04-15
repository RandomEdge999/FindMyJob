import { mkdir, rm, copyFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { transformAsync } from '@babel/core'
import presetReact from '@babel/preset-react'
import commonjs from '@rollup/plugin-commonjs'
import nodeResolve from '@rollup/plugin-node-resolve'
import { rollup } from 'rollup'

const rootDir = path.dirname(fileURLToPath(import.meta.url))
const sourceDir = path.join(rootDir, 'src')
const distDir = path.resolve(rootDir, '../src/findmyjob/web/frontend_dist')
const assetsDir = path.join(distDir, 'assets')

const cssShim = {
  name: 'css-shim',
  resolveId(source, importer) {
    if (!source.endsWith('.css') || !importer) return null
    return path.resolve(path.dirname(importer), source)
  },
  load(id) {
    if (!id.endsWith('.css')) return null
    return 'export default {};\n'
  },
}

const babelJsx = {
  name: 'babel-jsx',
  async transform(code, id) {
    if (!id.startsWith(sourceDir) || !/\.[jt]sx?$/.test(id)) {
      return null
    }
    const result = await transformAsync(code, {
      filename: id,
      babelrc: false,
      configFile: false,
      comments: false,
      compact: false,
      // Match Vite's default JSX behavior so JSX files do not need a manual
      // `import React` just to satisfy the production bundle.
      presets: [[presetReact, { runtime: 'automatic' }]],
    })
    return result ? { code: result.code ?? code, map: null } : null
  },
}

async function build() {
  await rm(distDir, { recursive: true, force: true })
  await mkdir(assetsDir, { recursive: true })

  const bundle = await rollup({
    input: path.join(sourceDir, 'main.jsx'),
    plugins: [
      cssShim,
      nodeResolve({
        browser: true,
        extensions: ['.mjs', '.js', '.jsx', '.json'],
      }),
      commonjs(),
      babelJsx,
    ],
    onwarn(warning, warn) {
      if (warning.code === 'THIS_IS_UNDEFINED') return
      warn(warning)
    },
  })

  await bundle.write({
    file: path.join(assetsDir, 'index.js'),
    format: 'es',
    sourcemap: false,
    banner: "var process = globalThis.process || { env: { NODE_ENV: 'production' } };",
  })
  await bundle.close()

  await copyFile(path.join(sourceDir, 'styles.css'), path.join(assetsDir, 'index.css'))
  await copyFile(path.join(sourceDir, 'runtime-fixes.js'), path.join(assetsDir, 'runtime-fixes.js'))

  await writeFile(
    path.join(distDir, 'index.html'),
    `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Find My Job Console</title>
    <script src="/assets/runtime-fixes.js"></script>
    <script type="module" src="/assets/index.js"></script>
    <link rel="stylesheet" href="/assets/index.css" />
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>
`,
    'utf-8',
  )
}

build().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
