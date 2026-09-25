const express = require('express');
const session = require('express-session');
const SQLiteStore = require('connect-sqlite3')(session);
const path = require('path');

const config = require('./config');
const security = require('./middleware/security');
const { httpLogger, logger } = require('./middleware/logger');

const app = express();

// Trust reverse proxy (Nginx) for correct IP and rate limiting
app.set('trust proxy', 1);

// 1. Logging Middleware
app.use(httpLogger);

// 2. Security Headers & Strict CORS
app.use(security.securityHeaders);
app.use(security.corsOptions);

// 3. Telegram Webhook Route (Must be parsed specifically for the bot)
if (config.TELEGRAM_MODE === 'webhook' && config.TELEGRAM_WEBHOOK_PATH) {
  const telegram = require('./services/telegram');
  app.post(
    config.TELEGRAM_WEBHOOK_PATH, 
    security.limiters.webhook, 
    security.verifyTelegramWebhook, 
    express.json({ limit: '5kb' }), 
    (req, res) => {
      telegram.bot.processUpdate(req.body);
      res.sendStatus(200);
    }
  );
}

// 4. Body Parser for Standard APIs
app.use(express.json({ limit: '10kb' }));

// 5. Secure Session Management (SQLite Persistent)
/*const DATA_DIR = config.NODE_ENV === 'production' ? '/data' : path.join(__dirname, '../data');
app.use(session({
  store: new SQLiteStore({ dir: DATA_DIR, db: 'sessions.db' }),
  secret: config.SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: {
    httpOnly: true,
    secure: config.NODE_ENV === 'production',
    sameSite: 'strict',
    maxAge: 8 * 60 * 60 * 1000 // 8 hours
  },
  rolling: true // Refresh session maxAge on activity
}));
// 5. Secure Session Management (SQLite Persistent)
const DATA_DIR = '/data'; // CRITICAL FIX: Hardcoded to the Docker persistent volume

app.use(session({
  store: new SQLiteStore({ dir: DATA_DIR, db: 'sessions.db' }),
  secret: config.SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: {
    httpOnly: true,
    secure: config.NODE_ENV === 'production',
    sameSite: 'lax', // CRITICAL FIX: Changed to 'lax' so Zerodha redirects don't drop the cookie
    maxAge: 8 * 60 * 60 * 1000 // 8 hours
  },
  rolling: true // Refresh session maxAge on activity
}));*/
// 5. Secure Session Management (SQLite Persistent)
const DATA_DIR = config.NODE_ENV === 'production' ? '/data' : path.join(__dirname, 'session_store');
const fs = require('fs');

// Ensure the directory exists so SQLite doesn't crash trying to make the file
if (!fs.existsSync(DATA_DIR)){
    fs.mkdirSync(DATA_DIR, { recursive: true });
}

app.use(session({
  store: new SQLiteStore({ dir: DATA_DIR, db: 'sessions.db' }),
  secret: config.SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: {
    httpOnly: true,
    secure: config.NODE_ENV === 'production',
    sameSite: 'lax', 
    maxAge: 8 * 60 * 60 * 1000 
  },
  rolling: true 
}));
// 6. API Routes
// Rate-limit login initiation inside the auth router.  The OAuth callback must
// remain reachable even when repeated login clicks exhaust that quota; sharing
// one limiter caused a valid Zerodha callback to be rejected with HTTP 429.
app.use('/api/auth', require('./routes/auth'));
app.use('/api/signals', security.limiters.signals, require('./routes/signals'));
app.use('/api/orders', security.limiters.orders, require('./routes/orders'));
app.use('/api/token', security.limiters.token, require('./routes/token'));
app.use('/api/proxy', require('./routes/proxy'));
app.use('/api/health', require('./routes/health'));
app.use('/api/internal', require('./routes/internal'));
// [SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers entry-signal receiver.
// Reuses the existing generic "webhook" limiter (100 req/min) rather than
// the equity-signal-specific one at /api/signals above.
app.use('/api/tv-fyers', security.limiters.webhook, require('./routes/tv-fyers-webhook'));
// Fyers OAuth login flow (separate broker/app from Zerodha's /api/auth).
app.use('/api/tv-fyers/auth', require('./routes/tv-fyers-auth'));


// 7. React Static File Serving
// [FIX 2026-09-14] The static dir resolution now handles BOTH
// the Docker layout (``/app/public/``, where the Dockerfile
// copies ``client/dist``) AND the local-dev layout
// (``../client/dist/``, where engineers run the server
// directly from the repo). Without this fallback, local-dev
// users see "login happened but dashboard is not visible"
// because ``app.get('*', ...)`` calls ``sendFile('public/index.html')``
// on a path that doesn't exist -- Express returns its default
// 500 page and the React bundle never loads.
//
// We pick the FIRST existing directory and log which one
// won. If neither exists we emit a loud startup-time ERROR
// rather than serving a broken app silently. ``fs`` is
// already imported at the top of this file (see DATA_DIR
// setup above).
const STATIC_DIR_CANDIDATES = [
  path.join(__dirname, 'public'),                       // Docker layout
  path.join(__dirname, '..', 'client', 'dist'),         // local dev
];
const STATIC_DIR = STATIC_DIR_CANDIDATES.find((candidate) => {
  try {
    return fs.existsSync(path.join(candidate, 'index.html'));
  } catch (_err) {
    return false;
  }
});
if (!STATIC_DIR) {
  // Fail loudly: the operator MUST see this at startup, not
  // discover it via a broken dashboard. We log via console
  // because the structured logger may not be initialised yet
  // at this point in the require graph (app.js loads before
  // middleware/logger.js finishes setting up). Both stderr
  // writes and a thrown error ensure the operator sees it.
  const tried = STATIC_DIR_CANDIDATES.join(', ');
  const msg = `[STARTUP ERROR] No built React bundle found. Tried: ${tried}. ` +
    `Run \`npm run build\` inside node-gateway/client/ to produce ` +
    `node-gateway/client/dist/index.html, or rebuild the Docker ` +
    `image (which copies it to server/public/).`;
// eslint-disable-next-line no-console
console.error(msg);
  throw new Error(msg);
}
// eslint-disable-next-line no-console
console.log(
  `[startup] Serving React bundle from: ${STATIC_DIR}`
);
app.use(express.static(STATIC_DIR));
app.get('*', (req, res) => {
  res.sendFile(path.join(STATIC_DIR, 'index.html'));
});

// 8. Global Error Handler
app.use((err, req, res, next) => {
  const statusCode = err.statusCode || 500;
  const type = err.type || 'internal_error';
  const clientMessage = err.clientMessage || 'An unexpected error occurred.';

  if (statusCode >= 500) {
    logger.error({ err, req_id: req.id, type }, 'Unhandled Exception');
  } else {
    // 4xx errors are logged as warnings
    logger.warn({ err_msg: err.message, req_id: req.id, type }, 'Client Error');
  }

  res.status(statusCode).json({
    error: type,
    message: clientMessage
  });
});

module.exports = app;
