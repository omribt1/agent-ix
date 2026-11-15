import { defineConfig } from 'vite';

// The dev server (app/dev_server.py) runs on :8000; proxying avoids CORS.
const target = process.env.AGENT_URL ?? 'http://localhost:8000';

export default defineConfig({
  server: { proxy: { '/a2a': target, '/assets': target } },
});
