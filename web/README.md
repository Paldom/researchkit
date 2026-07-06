# researchkit web

Minimal React frontend for researchkit. Single page: submit a topic (with keyword
generation, topic improvement, boost, and custom URLs), watch the run's live progress over
Server-Sent Events, then browse the output tabs — Report, Prompt, Raw JSON, Links, Log.
Past projects live in a collapsible sidebar.

## Stack

Vite + React 19 + TypeScript (strict) + Tailwind CSS v4. Native `fetch`/`EventSource`,
`react-markdown` + `remark-gfm` for reports. Vitest + Testing Library for tests.

## Development

```bash
npm install
npm run dev
```

`npm run dev` serves the app on Vite's dev server and proxies all `/api/*` requests to the
FastAPI backend at `http://localhost:8000` (see `server.proxy` in `vite.config.ts`), so start
the backend first. In production there is no proxy: the backend serves the built frontend
from `web/dist` on the same origin.

## Build

```bash
npm run build    # tsc --noEmit + vite build -> dist/
npm run preview  # serve the production build locally
```

## Checks

```bash
npm run lint          # eslint
npm run typecheck     # tsc --noEmit
npm test              # vitest run
npm run format        # prettier --write
npm run format:check
```

## Layout

```
src/
├── App.tsx                    # state + layout (sidebar, A4 reading frame)
├── components/
│   ├── ResearchForm.tsx       # topic / days / providers / preset / keywords / boost / URLs
│   ├── ProgressFeed.tsx       # live SSE progress feed
│   ├── OutputTabs.tsx         # Report | Prompt | Raw JSON | Links | Log
│   ├── ReportView.tsx         # markdown report + shared copy/download helpers
│   └── ProjectList.tsx        # past projects list (in the sidebar)
└── lib/
    ├── api.ts                 # API types + typed fetch helpers
    └── sse.ts                 # SSE payload parsing + run subscription
```
