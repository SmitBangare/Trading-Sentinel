// [SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers options pipeline,
// entry signal receiver. Mirrors routes/signals.js's shape (Zod validation,
// staleness/duplicate check, SQLite log, Telegram alert, forward to
// python-engine) with ONE deliberate difference: auth.
//
// TradingView's native webhook-alert feature cannot set custom HTTP
// headers or sign the request (no HMAC), so signals.js's
// verifySignalWebhook (X-Webhook-Signature header + HMAC) cannot be used
// here. Instead, TradingView embeds a shared secret as a FIELD inside the
// JSON alert body, and this route compares it with a constant-time string
// comparison. That secret is the entire auth story on TradingView's side --
// it sits in plaintext inside every alert message, which is the accepted
// trade-off given TradingView's constraints.
//
// No stop_loss/target fields in the payload: the bot derives and owns
// every exit itself (tv_fyers_orchestrator.py in python-engine).
// TradingView signals entries only.
const express = require('express');
const router = express.Router();
const { z } = require('zod');
const crypto = require('crypto');
const { v4: uuidv4 } = require('uuid');
const { signalsDb } = require('../db/index');
const telegram = require('../services/telegram');
const { logger } = require('../middleware/logger');
const { StaleSignalError } = require('../utils/errors');
const config = require('../config');

const tvSignalSchema = z.object({
  secret: z.string().min(1),
  symbol: z.string().min(2).max(20),
  direction: z.enum(['CE', 'PE']),
  underlying_price: z.number().positive().finite(),
  strategy: z.string().max(80).optional(),
  signal_time: z.string().datetime({ offset: true }),
});

// Constant-time compare of the shared secret embedded in the alert body.
// timingSafeEqual throws on mismatched buffer lengths, so length is
// checked first -- same defensive pattern signals.js's HMAC compare uses.
function verifyTvSharedSecret(req, res, next) {
  const sent = (req.body && req.body.secret) || '';
  const expected = config.TV_FYERS_WEBHOOK_SECRET;
  const sentBuf = Buffer.from(String(sent));
  const expectedBuf = Buffer.from(String(expected));
  const ok = sentBuf.length === expectedBuf.length
    && crypto.timingSafeEqual(sentBuf, expectedBuf);
  if (!ok) {
    return res.status(401).json({ error: 'unauthorized', message: 'Invalid or missing secret' });
  }
  next();
}

// Forward a validated, trusted signal into python-engine using the same
// internal-secret header every other node-gateway -> python-engine call
// uses (see routes/proxy.js's proxyToEngine). python-engine never sees
// the TradingView secret -- it only trusts node-gateway via this header.
async function forwardToEngine(signal) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.PYTHON_ENGINE_TIMEOUT_MS);
  try {
    const response = await fetch(`${config.PYTHON_ENGINE_URL}/internal/tv-fyers/signal`, {
      method: 'POST',
      headers: {
        'X-Internal-Secret': config.INTERNAL_API_SECRET,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(signal),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    return { ok: response.ok, status: response.status };
  } catch (err) {
    clearTimeout(timeout);
    logger.error({ event_type: 'tv_fyers_forward_error', err: err.message });
    return { ok: false, status: 0 };
  }
}

router.post('/webhook', verifyTvSharedSecret, async (req, res, next) => {
  try {
    const signal = tvSignalSchema.parse(req.body);
    const now = Date.now();
    const signalTimeMs = new Date(signal.signal_time).getTime();

    // Staleness: reuse the existing StaleSignalError, but the age budget
    // is a dedicated TV_FYERS setting, not the equity path's fixed 5 min
    // (mirrors config.TV_FYERS_SIGNAL_MAX_AGE_SEC on the python-engine
    // side; node-gateway does not import python-engine settings, so the
    // seconds value is inlined here -- keep the two in step manually).
    const maxAgeMs = 300 * 1000;
    if (now - signalTimeMs > maxAgeMs) {
      throw new StaleSignalError('TradingView signal exceeds max age');
    }

    // Duplicate check, same shape as signals.js.
    const isDuplicate = signalsDb.prepare(`
      SELECT 1 FROM tv_fyers_signals WHERE symbol = ? AND signal_time = ?
    `).get(signal.symbol, signal.signal_time);

    if (isDuplicate) {
      logger.info({ event_type: 'tv_fyers_duplicate_signal_dropped', symbol: signal.symbol });
      return res.status(200).json({ received: true, duplicate: true });
    }

    const signalId = uuidv4();
    const receivedAt = new Date().toISOString();
    // Never persist the secret itself.
    const { secret, ...persistable } = signal;

    signalsDb.prepare(`
      INSERT INTO tv_fyers_signals
        (signal_id, symbol, direction, underlying_price, strategy, signal_time, received_at, payload_json, forwarded, forward_status)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
    `).run(
      signalId, signal.symbol, signal.direction, signal.underlying_price,
      signal.strategy || null, signal.signal_time, receivedAt,
      JSON.stringify(persistable),
    );

    const forwardResult = await forwardToEngine({ ...persistable, signal_id: signalId });
    signalsDb.prepare(`
      UPDATE tv_fyers_signals SET forwarded = ?, forward_status = ? WHERE signal_id = ?
    `).run(forwardResult.ok ? 1 : 0, String(forwardResult.status), signalId);

    // sendAlert (unlike sendSignalAlert) returns a boolean, not a message
    // id -- there is no numeric id to persist here, only delivery success.
    const telegramSent = await telegram.sendAlert(
      `TV->Fyers signal: ${signal.symbol} ${signal.direction} @ ${signal.underlying_price}` +
      (forwardResult.ok ? '' : ' (forward to engine FAILED, check logs)')
    );
    signalsDb.prepare(`UPDATE tv_fyers_signals SET telegram_msg_id = ? WHERE signal_id = ?`)
      .run(telegramSent ? 1 : null, signalId);

    logger.info({
      event_type: 'tv_fyers_signal_received', signalId,
      symbol: signal.symbol, direction: signal.direction, forwarded: forwardResult.ok,
    });
    res.status(200).json({ received: true, signal_id: signalId, forwarded: forwardResult.ok });
  } catch (err) {
    next(err);
  }
});

module.exports = router;
