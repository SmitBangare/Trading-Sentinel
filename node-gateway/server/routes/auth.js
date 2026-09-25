const crypto = require('crypto');
const express = require('express');
const router = express.Router();
const kite = require('../services/kite');
const tokenStore = require('../services/token-store');
const config = require('../config');
const { logger } = require('../middleware/logger');
const { requireSession } = require('../middleware/auth');
const { limiters } = require('../middleware/security');
const { withRetry } = require('../utils/retry');

// [SMIT-FYERS-OPTIONS 2026-09-25] Dashboard login without Zerodha. Sets the
// exact same req.session.authenticated flag the Zerodha callback below
// sets, so requireSession/proxy.js/everything downstream is unchanged --
// this is just a second way to reach that same flag, not a new auth model.
router.post('/password-login', limiters.authLogin, (req, res) => {
  const { password } = req.body || {};
  const expected = Buffer.from(config.DASHBOARD_PASSWORD);
  const supplied = Buffer.from(String(password || ''));
  const match = expected.length === supplied.length && crypto.timingSafeEqual(expected, supplied);
  if (!match) {
    logger.warn({ event_type: 'dashboard_password_login_failed' }, 'Dashboard password login failed');
    return res.status(401).json({ success: false, message: 'Incorrect password' });
  }
  req.session.authenticated = true;
  req.session.login_time = new Date().toISOString();
  logger.info({ event_type: 'dashboard_password_login_success' }, 'Dashboard password login succeeded');
  res.json({ success: true });
});

// Generate Zerodha login URL
router.get('/login', limiters.authLogin, (req, res) => {
  logger.info({ event_type: 'auth_initiated' }, 'Zerodha OAuth initiated');
  res.redirect(kite.getLoginURL());
});

// OAuth Callback from Zerodha
router.get('/callback', limiters.authCallback, async (req, res, next) => {
  try {
    const { request_token, status } = req.query;

    if (status !== 'success' || !request_token) {
      logger.warn({ event_type: 'auth_failed', status }, 'Zerodha login failed or denied');
      return res.redirect('/login?error=zerodha_failed');
    }

    // Generate session token
    const accessToken = await kite.generateSession(request_token, config.ZERODHA_API_SECRET);
    tokenStore.setToken(accessToken);

    // Setup secure session
    req.session.authenticated = true;
    req.session.login_time = new Date().toISOString();

    // Provision token to Container B (Non-fatal if B is unreachable)
    try {
      await withRetry(async () => {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 2000);
        const resp = await fetch(`${config.PYTHON_ENGINE_URL}/token`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Internal-Secret': config.INTERNAL_API_SECRET
          },
          body: JSON.stringify({ access_token: accessToken }),
          signal: controller.signal
        });
        clearTimeout(timeout);
        if (!resp.ok) throw new Error(`Engine returned ${resp.status}`);
      }, 3, 1000);
    } catch (err) {
      logger.warn({ event_type: 'engine_provision_failed', err: err.message }, 'Failed to provision token to Container B');
    }

    // Notify via Telegram
    const telegram = require('../services/telegram');
    telegram.sendAlert('✅ Zerodha authenticated successfully. System ready.');
    logger.info({ event_type: 'auth_success' }, 'Zerodha login complete');

    res.redirect('/');
  } catch (err) {
    next(err);
  }
});

// Check current auth status
router.get('/status', (req, res) => {
  const tokenInfo = tokenStore.getStatus();
  let ageMinutes = null;
  
  if (tokenInfo.generatedAt) {
    ageMinutes = Math.floor((Date.now() - new Date(tokenInfo.generatedAt).getTime()) / 60000);
  }

  res.json({
    authenticated: req.session?.authenticated || false,
    login_time: req.session?.login_time || null,
    token_age_min: ageMinutes
  });
});

// Logout and clear tokens
router.post('/logout', requireSession, async (req, res) => {
  tokenStore.clearToken();
  
  // Notify Container B to void token
  try {
    await fetch(`${config.PYTHON_ENGINE_URL}/token/invalidate`, {
      method: 'POST',
      headers: { 'X-Internal-Secret': config.INTERNAL_API_SECRET }
    });
  } catch (err) {
    logger.warn({ event_type: 'engine_invalidate_failed' }, 'Could not notify Container B of logout');
  }

  req.session.destroy(() => {
    res.json({ success: true, message: 'Logged out successfully' });
  });
});

module.exports = router;
