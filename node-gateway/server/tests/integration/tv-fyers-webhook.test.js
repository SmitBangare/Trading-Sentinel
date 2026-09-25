/**
 * Integration tests for routes/tv-fyers-webhook.js
 *
 * Tests POST /api/tv-fyers/webhook:
 * - shared-secret body-field verification (constant-time, NOT HMAC --
 *   TradingView cannot set custom headers or sign requests)
 * - Zod schema validation
 * - staleness check
 * - duplicate detection
 * - forward to python-engine with X-Internal-Secret header
 * - Telegram notification
 *
 * Mirrors tests/integration/signals.test.js's mocking conventions.
 */

const mockSendAlert = jest.fn().mockResolvedValue(true);

jest.mock('../../services/telegram', () => ({
  bot: { on: jest.fn(), sendMessage: jest.fn() },
  isValidChat: jest.fn(() => true),
  sendSignalAlert: jest.fn(),
  sendAlert: mockSendAlert,
}));

const mockPrepare = jest.fn();
const mockRun = jest.fn();
const mockGet = jest.fn();
mockPrepare.mockReturnValue({ run: mockRun, get: mockGet });

jest.mock('../../db/index', () => ({
  signalsDb: { prepare: mockPrepare },
  appDb: { prepare: mockPrepare },
}));

const TV_SECRET = 'test_tv_fyers_webhook_secret_32chars_long';

jest.mock('../../config', () => ({
  TELEGRAM_CHAT_ID: '99999999999',
  TELEGRAM_BOT_TOKEN: 'fake-token',
  TELEGRAM_MODE: 'polling',
  ALLOWED_ORIGINS: ['http://localhost:3001'],
  PORT: 3001,
  NODE_ENV: 'test',
  ZERODHA_API_KEY: 'fake_key',
  ZERODHA_API_SECRET: 'fake_secret',
  ZERODHA_REDIRECT_URL: 'http://localhost:3001/callback',
  SESSION_SECRET: 'test_session_secret_32chars_long!!',
  INTERNAL_API_SECRET: 'test_internal_secret_32chars_long',
  OPENCLAW_WEBHOOK_SECRET: 'test_openclaw_secret_32chars_long',
  TV_FYERS_WEBHOOK_SECRET: TV_SECRET,
  PYTHON_ENGINE_URL: 'http://localhost:8000',
  PYTHON_ENGINE_TIMEOUT_MS: 5000,
  LOG_LEVEL: 'error',
  RATE_LIMIT_WINDOW_MS: 60000,
  RATE_LIMIT_MAX: 1000,
}));

const express = require('express');
const request = require('supertest');

let app;
let fetchMock;

beforeAll(() => {
  app = express();
  app.use(express.json());
  app.use('/api/tv-fyers', require('../../routes/tv-fyers-webhook'));
  app.use((err, req, res, next) => {
    const statusCode = err.statusCode || 500;
    res.status(statusCode).json({ error: err.type || 'error', message: err.message });
  });
});

beforeEach(() => {
  jest.clearAllMocks();
  mockGet.mockReturnValue(null); // no duplicates by default
  mockRun.mockReturnValue({});
  fetchMock = jest.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  global.fetch = fetchMock;
});

function makeSignal(overrides = {}) {
  return {
    secret: TV_SECRET,
    symbol: 'NIFTY',
    direction: 'CE',
    underlying_price: 24500.5,
    strategy: 'orb_momentum_v1',
    signal_time: new Date().toISOString(),
    ...overrides,
  };
}

describe('POST /api/tv-fyers/webhook', () => {
  test('accepts a valid signal with the correct shared secret', async () => {
    const res = await request(app).post('/api/tv-fyers/webhook').send(makeSignal());
    expect(res.status).toBe(200);
    expect(res.body.received).toBe(true);
    expect(res.body.signal_id).toBeDefined();
  });

  test('rejects missing secret', async () => {
    const { secret, ...noSecret } = makeSignal();
    const res = await request(app).post('/api/tv-fyers/webhook').send(noSecret);
    expect(res.status).toBe(401);
    expect(res.body.error).toBe('unauthorized');
  });

  test('rejects wrong secret', async () => {
    const res = await request(app)
      .post('/api/tv-fyers/webhook')
      .send(makeSignal({ secret: 'wrong-secret-value-here-not-matching' }));
    expect(res.status).toBe(401);
  });

  test('rejects malformed payload (bad direction)', async () => {
    const res = await request(app)
      .post('/api/tv-fyers/webhook')
      .send(makeSignal({ direction: 'LONG' }));
    expect(res.status).toBe(500); // ZodError falls to the generic error handler
  });

  test('rejects a stale signal', async () => {
    const staleTime = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const res = await request(app)
      .post('/api/tv-fyers/webhook')
      .send(makeSignal({ signal_time: staleTime }));
    expect(res.status).toBe(422);
    expect(res.body.error).toBe('stale_signal');
  });

  test('drops a duplicate signal without re-forwarding', async () => {
    mockGet.mockReturnValue({ 1: 1 }); // duplicate found
    const res = await request(app).post('/api/tv-fyers/webhook').send(makeSignal());
    expect(res.status).toBe(200);
    expect(res.body.duplicate).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test('forwards to python-engine with X-Internal-Secret header, no secret leaked', async () => {
    await request(app).post('/api/tv-fyers/webhook').send(makeSignal());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe('http://localhost:8000/internal/tv-fyers/signal');
    expect(options.headers['X-Internal-Secret']).toBe('test_internal_secret_32chars_long');
    const forwardedBody = JSON.parse(options.body);
    expect(forwardedBody.secret).toBeUndefined();
    expect(forwardedBody.symbol).toBe('NIFTY');
  });

  test('sends a Telegram alert on a fresh signal', async () => {
    await request(app).post('/api/tv-fyers/webhook').send(makeSignal());
    expect(mockSendAlert).toHaveBeenCalledTimes(1);
    expect(mockSendAlert.mock.calls[0][0]).toContain('NIFTY');
  });

  test('still records the signal (as received) if the forward to python-engine fails', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 502, json: async () => ({}) });
    const res = await request(app).post('/api/tv-fyers/webhook').send(makeSignal());
    expect(res.status).toBe(200);
    expect(res.body.forwarded).toBe(false);
  });
});
