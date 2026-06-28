import { defineConfig } from '@kubb/core'
import { pluginClient } from '@kubb/plugin-client'
import { pluginOas } from '@kubb/plugin-oas'
import { pluginReactQuery } from '@kubb/plugin-react-query'
import { pluginTs } from '@kubb/plugin-ts'

// Generates the TS types, a typed fetch client and TanStack Query hooks from the
// Django Ninja OpenAPI schema. Regenerate with `npm run openapi && npm run generate`.
export default defineConfig({
  root: '.',
  input: { path: './openapi.json' },
  output: { path: './src/gen', clean: true },
  plugins: [
    pluginOas(),
    pluginTs(),
    pluginClient({ baseURL: '/api', dataReturnType: 'data' }),
    pluginReactQuery({ baseURL: '/api', dataReturnType: 'data' }),
  ],
})
