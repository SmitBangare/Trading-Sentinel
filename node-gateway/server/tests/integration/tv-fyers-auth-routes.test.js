/**
 * Integration tests for routes/tv-fyers-auth.js -- /login, /callback,
 * /status, /logout. Mocks services/tv-fyers-auth.js and
 * services/tv-fyers-token-store.js so no real Fyers/network calls happen.
 */
const mockGetLoginURL = jest.fn(() => 'https://api-t1.fyers.in/api/v3/generate-authcode?mocked=1');
const mockGenerateSession = jest.fn();

jest.mock('../../services/tv-fyers-auth', () => ({
  getLoginURL: mockGetLoginURL,
  generateSession: mockGenerateSession,
}));

const mockSendAlert = jest.fn().mockResolvedValue(true);
jest.mock('../../services/telegram', () => ({
  bot: { on: jest.fn(), sendMessage: jest.fn() },
  isValidChat: jest.fn(() => true),
  sendSignalAlert: jest.fn(),
  sendAlert: mockSendAlert,
}));

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
  TV_FYERS_WEBHOOK_SECRET: 'test_tv_fyers_webhook_secret_32chars_long',
  FYERS_CLIENT_ID: 'test-client-100',
  FYERS_SECRET_KEY: 'test-secret-key',
  FYERS_REDIRECT_URL: 'http://localhost:3001/api/tv-fyers/auth/callback',
  PYTHON_ENGINE_URL: 'http://localhost:8000',
  PYTHON_ENGINE_TIMEOUT_MS: 5000,
  LOG_LEVEL: 'silent',
  RATE_LIMIT_WINDOW_MS: 60000,
  RATE_LIMIT_MAX: 1000,
}));

const express = require('express');
const session = require('express-session');
const request = require('supertest');
const tokenStore = require('../../services/tv-fyers-token-store');

let app;
let fetchMock;

beforeAll(() => {
  app = express();
  app.use(express.json());
  app.use(session({ secret: 'test', resave: false, saveUninitialized: true }));
  app.use('/api/tv-fyers/auth', require('../../routes/tv-fyers-auth'));
  app.use((err, req, res, next) => {
    const statusCode = err.statusCode || 500;
    res.status(statusCode).json({ error: err.type || 'error', message: err.message });
  });
});

beforeEach(() => {
  jest.clearAllMocks();
  tokenStore.clearToken();
  fetchMock = jest.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  global.fetch = fetchMock;
});

describe('GET /api/tv-fyers/auth/login', () => {
  test('redirects to the Fyers login URL', async () => {
    const res = await request(app).get('/api/tv-fyers/auth/login');
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('api-t1.fyers.in');
    expect(mockGetLoginURL).toHaveBeenCalledTimes(1);
  });
});

describe('GET /api/tv-fyers/auth/callback', () => {
  test('exchanges auth_code, stores the token, and provisions python-engine', async () => {
    mockGenerateSession.mockResolvedValue('fyers-access-token-abc');
    const res = await request(app).get('/api/tv-fyers/auth/callback?auth_code=xyz&s=ok');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/');
    expect(mockGenerateSession).toHaveBeenCalledWith('xyz');
    expect(tokenStore.getToken()).toBe('fyers-access-token-abc');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe('http://localhost:8000/internal/tv-fyers/token');
    expect(options.headers['X-Internal-Secret']).toBe('test_internal_secret_32chars_long');
    expect(JSON.parse(options.body)).toEqual({ access_token: 'fyers-access-token-abc' });
  });

  test('redirects with an error when auth_code is missing', async () => {
    const res = await request(app).get('/api/tv-fyers/auth/callback?s=error');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/?error=fyers_failed');
    expect(mockGenerateSession).not.toHaveBeenCalled();
  });

  test('still redirects home even if python-engine provisioning fails (non-fatal)', async () => {
    mockGenerateSession.mockResolvedValue('fyers-access-token-abc');
    fetchMock.mockRejectedValue(new Error('engine unreachable'));
    const res = await request(app).get('/api/tv-fyers/auth/callback?auth_code=xyz&s=ok');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/');
    expect(tokenStore.getToken()).toBe('fyers-access-token-abc'); // still stored locally
  });
});

describe('GET /api/tv-fyers/auth/status', () => {
  test('reports not authenticated with no session', async () => {
    const res = await request(app).get('/api/tv-fyers/auth/status');
    expect(res.status).toBe(200);
    expect(res.body.authenticated).toBe(false);
  });
});

describe('POST /api/tv-fyers/auth/logout', () => {
  test('requires an authenticated session', async () => {
    const res = await request(app).post('/api/tv-fyers/auth/logout');
    expect(res.status).toBe(401);
  });
});
