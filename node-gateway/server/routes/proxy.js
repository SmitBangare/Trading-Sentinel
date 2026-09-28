const express = require('express');
const router = express.Router();
const config = require('../config');
const { requireSession } = require('../middleware/auth');
const { logger } = require('../middleware/logger');

// Generic Proxy function
const proxyToEngine = async (req, res, path, method = 'GET', options = {}) => {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs || config.PYTHON_ENGINE_TIMEOUT_MS;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const fetchOptions = {
      method,
      headers: {
        'X-Internal-Secret': config.INTERNAL_API_SECRET,
        'Content-Type': 'application/json'
      },
      signal: controller.signal
    };

    if (method !== 'GET' && method !== 'HEAD' && req.body) {
      fetchOptions.body = JSON.stringify(req.body);
    }

    const response = await fetch(`${config.PYTHON_ENGINE_URL}${path}`, fetchOptions);
    clearTimeout(timeout);

    const data = await response.json();
    
    // Security: Strip internal headers from response if any exist
    res.removeHeader('x-internal-secret'); 
    
    res.status(response.status).json(data);
  } catch (err) {
    clearTimeout(timeout);
    logger.error({ event_type: 'proxy_error', path, err: err.message });
    res.status(502).json({ error: 'bad_gateway', message: 'Quant Engine is unreachable.' });
  }
};

router.use(requireSession);

const withQuery = (path, query) => {
  const params = new URLSearchParams();
  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  });
  const suffix = params.toString();
  return suffix ? `${path}?${suffix}` : path;
};

// Map of endpoints to proxy
router.get('/signals', (req, res) => proxyToEngine(req, res, '/signals'));
router.get('/rejected', (req, res) => proxyToEngine(req, res, '/rejected'));
router.get('/positions', (req, res) => proxyToEngine(req, res, '/positions'));
router.get('/performance', (req, res) => proxyToEngine(req, res, '/performance'));
router.get('/performance/divisions', (req, res) => proxyToEngine(req, res, '/performance/divisions'));
router.get('/health-b', (req, res) => proxyToEngine(req, res, '/health'));
router.get('/bankroll', (req, res) => proxyToEngine(req, res, '/bankroll'));
router.get('/circuit-breaker', (req, res) => proxyToEngine(req, res, '/circuit-breaker'));
router.post('/circuit-breaker/reset', (req, res) => proxyToEngine(req, res, '/circuit-breaker/reset', 'POST'));
router.get('/experiments/momentum', (req, res) => proxyToEngine(req, res, '/experiments/momentum'));
router.get('/experiments/penny', (req, res) => proxyToEngine(req, res, '/experiments/penny'));
router.get('/experiments/fno-opening-range', (req, res) =>
  proxyToEngine(req, res, '/experiments/fno-opening-range'));
router.get('/research/promotion-readiness', (req, res) =>
  proxyToEngine(req, res, '/research/promotion-readiness'));
router.get('/analytics/proactive-activity', (req, res) =>
  proxyToEngine(req, res, withQuery('/analytics/proactive-activity', req.query)));
router.get('/analytics/proactive-comparison', (req, res) =>
  proxyToEngine(req, res, withQuery('/analytics/proactive-comparison', req.query)));
router.get('/analytics/proactive-research-comparison', (req, res) =>
  proxyToEngine(req, res, withQuery('/analytics/proactive-research-comparison', req.query)));
router.get('/analytics/optional-ai-status', (req, res) =>
  proxyToEngine(req, res, '/analytics/optional-ai-status'));
router.get('/analytics/proactive-session-diagnostics', (req, res) =>
  proxyToEngine(req, res, withQuery('/analytics/proactive-session-diagnostics', req.query)));
router.get('/analytics/scheduler-timing', (req, res) =>
  proxyToEngine(req, res, withQuery('/analytics/scheduler-timing', req.query)));
router.get('/analytics/operational-coverage', (req, res) =>
  proxyToEngine(req, res, '/analytics/operational-coverage'));
router.get('/analytics/reconciliation-evidence', (req, res) =>
  proxyToEngine(req, res, withQuery('/analytics/reconciliation-evidence', req.query)));
router.get('/partner/hedge/cards', (req, res) =>
  proxyToEngine(req, res, withQuery('/partner/hedge/cards', req.query)));
router.get('/partner/hedge/delivery-backlog', (req, res) =>
  proxyToEngine(req, res, '/partner/hedge/delivery-backlog'));
router.get('/partner/advisory/setup', (req, res) =>
  proxyToEngine(req, res, withQuery('/partner/advisory/setup', req.query)));
router.get('/partner/advisory/diagnostics', (req, res) =>
  proxyToEngine(req, res, '/partner/advisory/diagnostics'));
router.get('/partner/advisory/research-readiness', (req, res) =>
  proxyToEngine(req, res, '/partner/advisory/research-readiness'));
router.put('/partner/advisory/profile', (req, res) =>
  proxyToEngine(req, res, withQuery('/partner/advisory/profile', req.query), 'PUT'));

// [SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers dashboard panel reads.
router.get('/tv-fyers/status', (req, res) => proxyToEngine(req, res, '/internal/tv-fyers/status'));
router.get('/tv-fyers/signals', (req, res) =>
  proxyToEngine(req, res, withQuery('/internal/tv-fyers/signals', req.query)));
router.get('/tv-fyers/positions', (req, res) =>
  proxyToEngine(req, res, withQuery('/internal/tv-fyers/positions', req.query)));
// A fresh (uncached) fetch chunks a year of Fyers history across several
// real network calls -- a longer, dedicated budget, separate from the
// generic backtestTimeoutMs below (which this route predates using).
router.post('/tv-fyers/backtest/run', (req, res) =>
  proxyToEngine(req, res, withQuery('/internal/tv-fyers/backtest/run', req.query), 'POST',
    { timeoutMs: 120000 }));

// Backtest Lab submits background work, so requests never hold an HTTP socket
// for the duration of a replay. The longer budget protects SQLite contention
// and larger persisted result reads without weakening timeouts globally.
const backtestTimeoutMs = Math.max(config.PYTHON_ENGINE_TIMEOUT_MS, 30000);

router.get('/backtests/strategies', (req, res) =>
  proxyToEngine(req, res, '/backtests/strategies', 'GET', { timeoutMs: backtestTimeoutMs }));
router.post('/backtests/runs', (req, res) =>
  proxyToEngine(req, res, '/backtests/runs', 'POST', { timeoutMs: backtestTimeoutMs }));
router.get('/backtests/runs', (req, res) =>
  proxyToEngine(req, res, withQuery('/backtests/runs', req.query), 'GET', { timeoutMs: backtestTimeoutMs }));
router.get('/backtests/runs/:runId', (req, res) =>
  proxyToEngine(req, res, `/backtests/runs/${encodeURIComponent(req.params.runId)}`, 'GET',
    { timeoutMs: backtestTimeoutMs }));

module.exports = router;
