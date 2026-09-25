const { z } = require('zod');
require('dotenv').config();

const configSchema = z.object({
  NODE_ENV: z.enum(['production', 'development']).default('production'),
  PORT: z.coerce.number().int().default(3000),
  ALLOWED_ORIGINS: z.string().transform((val) => val.split(',').map(v => v.trim())),
  
  ZERODHA_API_KEY: z.string().min(8),
  ZERODHA_API_SECRET: z.string().min(8),
  ZERODHA_REDIRECT_URL: z.string().url().endsWith('/api/auth/callback'),
  
  TELEGRAM_BOT_TOKEN: z.string().regex(/^\d+:[A-Za-z0-9_-]+$/),
  TELEGRAM_CHAT_ID: z.string().min(5),
  TELEGRAM_WEBHOOK_SECRET: z.string().min(32),
  TELEGRAM_MODE: z.enum(['webhook', 'polling']).default('webhook'),
  TELEGRAM_WEBHOOK_PATH: z.string().startsWith('/tg-hook/').optional(),
  
  SESSION_SECRET: z.string().min(32),
  INTERNAL_API_SECRET: z.string().min(32),
  OPENCLAW_WEBHOOK_SECRET: z.string().min(32),
  // Shared secret TradingView embeds in its Pine Script alert body (TradingView
  // webhook alerts cannot set custom headers or sign requests, so a body-field
  // shared secret is the only auth mechanism available on that side -- see
  // routes/tv-fyers-webhook.js). Must equal python-engine's TV_FYERS_WEBHOOK_SECRET.
  TV_FYERS_WEBHOOK_SECRET: z.string().min(32),

  // Fyers OAuth app credentials (from https://myapi.fyers.in/dashboard/),
  // separate app/broker from Zerodha -- see routes/tv-fyers-auth.js.
  FYERS_CLIENT_ID: z.string().min(1),
  FYERS_SECRET_KEY: z.string().min(1),
  FYERS_REDIRECT_URL: z.string().url(),
  
  PYTHON_ENGINE_URL: z.string().url().default('http://python-engine:8000'),
  PYTHON_ENGINE_TIMEOUT_MS: z.coerce.number().int().positive().default(5000),
  
  // [FILL-ANCHOR 2026-08-04] Stop floors, mirrored from the engine's
  // MOMENTUM_MIN_STOP_PCT / MOMENTUM_MIN_STOP_ATR_MULT. The gateway re-applies
  // them against the transaction price because a floor computed on a stale
  // close is not a floor on the fill. Keep these in step with config.py --
  // divergence shows up as a stop that the engine's exit logic disagrees with.
  MOMENTUM_MIN_STOP_PCT: z.coerce.number().positive().default(0.012),
  MOMENTUM_MIN_STOP_ATR_MULT: z.coerce.number().nonnegative().default(0.35),

  LOG_LEVEL: z.enum(['trace', 'debug', 'info', 'warn', 'error', 'fatal']).default('info'),
  RATE_LIMIT_WINDOW_MS: z.coerce.number().int().positive().default(60000),
  RATE_LIMIT_MAX: z.coerce.number().int().positive().default(100)
});

let config;
try {
  config = configSchema.parse(process.env);
} catch (err) {
  console.error('❌ FATAL: Environment validation failed:');
  err.errors.forEach(e => console.error(`   - ${e.path.join('.')}: ${e.message}`));
  process.exit(1); // The only time process.exit() is allowed: Pre-startup.
}

module.exports = config;
