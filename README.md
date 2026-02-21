# Parliament Navigator

A modern web application for exploring UK Parliament debates, tracking MPs and Lords, and analyzing parliamentary discussions by topic.

## What This App Does

- **Dashboard**: Browse recent parliamentary debates from the House of Commons and House of Lords
- **Debate View**: Read full debate transcripts with speaker filtering and search
- **Topic Intelligence**: Create topic groups with keywords and analyze which MPs speak most about those topics
- **Member Tracker**: Search for MPs/Lords, add them to a watchlist, and view their recent contributions

## Prerequisites

Before you start, make sure you have these installed on your computer:

### 1. Node.js (Required)

Node.js is the JavaScript runtime that runs this application.

**To check if you have it:**
```bash
node --version
```

If you see a version number (like `v20.10.0`), you're good. If not, download and install it from:
- https://nodejs.org/ (choose the LTS version)

### 2. npm (Comes with Node.js)

npm is the package manager for installing dependencies.

**To check if you have it:**
```bash
npm --version
```

## Quick Start

### Step 1: Install Dependencies

Open a terminal/command prompt in this folder and run:

```bash
npm install
```

This downloads all the libraries the app needs. It might take a minute or two.

### Step 2: Start the Development Server

```bash
npm run dev
```

You should see output like:
```
VITE v7.x.x  ready in 300 ms

➜  Local:   http://localhost:5173/
```

### Step 3: Open in Browser

Open your web browser and go to the URL shown (usually http://localhost:5173/).

That's it! You should see the Parliament Navigator dashboard.

## Available Commands

Run these in your terminal from the project folder:

| Command | What it does |
|---------|--------------|
| `npm run dev` | Starts the development server (use this for local development) |
| `npm run build` | Creates a production build in the `dist/` folder |
| `npm run preview` | Preview the production build locally |
| `npm run test` | Run tests in watch mode (re-runs when files change) |
| `npm run test:run` | Run tests once and exit |
| `npm run lint` | Check code for style issues |
| `npm run format` | Auto-format all code with Prettier |
| `npm run typecheck` | Check TypeScript types without building |

## Project Structure

```
hansard/
├── src/                    # All the application source code
│   ├── components/         # Reusable UI components
│   │   ├── ui/            # Generic UI components (LoadingSpinner, etc.)
│   │   ├── DebateCard.tsx
│   │   ├── MemberCard.tsx
│   │   └── ...
│   ├── pages/             # Main page components (routes)
│   │   ├── Dashboard.tsx
│   │   ├── DebateView.tsx
│   │   ├── TopicIntelligence.tsx
│   │   └── MemberTracker.tsx
│   ├── hooks/             # Custom React hooks
│   │   ├── useDebates.ts
│   │   ├── useWatchlist.ts
│   │   └── useTopicGroups.ts
│   ├── services/          # API calls and data fetching
│   │   ├── hansardApi.ts
│   │   └── membersApi.ts
│   ├── types/             # TypeScript type definitions
│   ├── config/            # Configuration files
│   ├── utils/             # Utility functions
│   └── test/              # Test setup and mocks
├── functions/             # Cloudflare serverless functions (API proxy)
│   ├── api/              # API endpoint handlers
│   └── lib/              # Shared utilities for functions
├── public/               # Static files (favicon, etc.)
├── dist/                 # Production build output (generated)
├── package.json          # Project dependencies and scripts
├── tsconfig.json         # TypeScript configuration
├── vite.config.js        # Vite bundler configuration
└── vitest.config.ts      # Test configuration
```

## How the App Works

### Data Sources

The app fetches data from official UK Parliament APIs:
- **Hansard API**: Debate transcripts and search (https://hansard-api.parliament.uk)
- **Members API**: MP/Lord information (https://members-api.parliament.uk)

### Key Features Explained

**Dashboard**
- Shows recent debates grouped by date
- Filter by House of Commons or House of Lords
- Click any debate to view the full transcript

**Debate View**
- Full transcript with speaker photos and party colors
- Search within the transcript
- Filter by specific speaker
- Uses virtualization for smooth scrolling through long debates

**Topic Intelligence**
- Create "topic groups" with related keywords (e.g., "Climate" with keywords: "climate change", "net zero", etc.)
- The app searches for all keywords and shows which MPs mention them most
- Export results to CSV

**Member Tracker**
- Search for any MP or Lord by name
- Add members to your watchlist (saved in browser)
- View their recent contributions in Parliament

### Data Caching

The app uses React Query to cache API responses:
- Debates: Cached for 5 minutes
- Member info: Cached for 15 minutes
- Search results: Cached for 2 minutes

This means pages load faster on repeat visits and reduces API calls.

## Development Guide

### Making Changes

1. Edit files in the `src/` folder
2. The browser automatically reloads when you save
3. Check the terminal for any errors

### Running Tests

```bash
# Run tests and watch for changes
npm run test

# Run tests once
npm run test:run

# Run with coverage report
npm run test:coverage
```

### Type Checking

The project uses TypeScript for type safety. Run:

```bash
npm run typecheck
```

This catches type errors without building the whole project.

### Code Formatting

Format all code automatically:

```bash
npm run format
```

Check formatting without changing files:

```bash
npm run format:check
```

## Deployment

### Building for Production

```bash
npm run build
```

This creates optimized files in the `dist/` folder.

### Deploying to Cloudflare Pages

The app is configured for Cloudflare Pages deployment:

```bash
npm run deploy
```

This builds and deploys to Cloudflare. You'll need to:
1. Have a Cloudflare account
2. Be logged in via `wrangler login`

## Troubleshooting

### "npm: command not found"

Node.js isn't installed or isn't in your PATH. Install it from https://nodejs.org/

### "Port 5173 is in use"

Another app is using that port. Either:
- Close the other app
- Vite will automatically try the next port (5174, 5175, etc.)

### "Module not found" errors

Run `npm install` to make sure all dependencies are installed.

### API errors / No data showing

The Parliament APIs are external services. If they're down or slow:
- Check https://hansard-api.parliament.uk/swagger/ui/index to see if the API is working
- Try refreshing the page
- The app will retry failed requests automatically

### TypeScript errors

Run `npm run typecheck` to see all type errors. Common fixes:
- Make sure you're importing types correctly
- Check that function parameters match their types

## Tech Stack

- **React 19** - UI framework
- **TypeScript** - Type safety
- **Vite** - Build tool and dev server
- **Tailwind CSS 4** - Styling
- **React Query** - Data fetching and caching
- **React Router** - Page navigation
- **React Virtual** - List virtualization for performance
- **DOMPurify** - HTML sanitization for security
- **Vitest** - Testing framework
- **Cloudflare Pages** - Hosting (optional)

## License

This project uses publicly available UK Parliament data. The Parliament APIs are free to use and don't require authentication.
