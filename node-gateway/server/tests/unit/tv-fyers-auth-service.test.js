/**
 * Unit tests for services/tv-fyers-auth.js -- getLoginURL/generateSession.
 * Mocks the `https` module so no real network calls are made.
 */
jest.mock('../../config', () => ({
  FYERS_CLIENT_ID: 'test-client-100',
  FYERS_SECRET_KEY: 'test-secret-key',
  FYERS_REDIRECT_URL: 'http://localhost:3000/api/tv-fyers/auth/callback',
      LOG_LEVEL: 'silent',
}));

const { EventEmitter } = require('events');

function mockHttpsRequest(statusCode, responseBody) {
  return jest.fn((options, callback) => {
    const res = new EventEmitter();
    res.statusCode = statusCode;
    const req = new EventEmitter();
    req.write = jest.fn();
    req.end = jest.fn(() => {
      callback(res);
      res.emit('data', JSON.stringify(responseBody));
      res.emit('end');
    });
    return req;
  });
}

describe('getLoginURL', () => {
  test('builds the documented Fyers generate-authcode URL', () => {
    const fyersAuth = require('../../services/tv-fyers-auth');
    const url = fyersAuth.getLoginURL();
    expect(url).toContain('https://api-t1.fyers.in/api/v3/generate-authcode');
    expect(url).toContain('client_id=test-client-100');
    expect(url).toContain('response_type=code');
    expect(url).toContain(encodeURIComponent('http://localhost:3000/api/tv-fyers/auth/callback'));
  });
});

describe('generateSession', () => {
  beforeEach(() => {
    jest.resetModules();
  });

  test('exchanges auth_code for an access_token on success', async () => {
    jest.doMock('https', () => ({
      request: mockHttpsRequest(200, { s: 'ok', access_token: 'fyers-access-token-xyz' }),
    }));
    jest.doMock('../../config', () => ({
      FYERS_CLIENT_ID: 'test-client-100',
      FYERS_SECRET_KEY: 'test-secret-key',
      FYERS_REDIRECT_URL: 'http://localhost:3000/api/tv-fyers/auth/callback',
      LOG_LEVEL: 'silent',
    }));
    const fyersAuth = require('../../services/tv-fyers-auth');
    const token = await fyersAuth.generateSession('some-auth-code');
    expect(token).toBe('fyers-access-token-xyz');
  });

  test('throws when Fyers reports an error status', async () => {
    jest.doMock('https', () => ({
      request: mockHttpsRequest(200, { s: 'error', message: 'invalid auth code' }),
    }));
    jest.doMock('../../config', () => ({
      FYERS_CLIENT_ID: 'test-client-100',
      FYERS_SECRET_KEY: 'test-secret-key',
      FYERS_REDIRECT_URL: 'http://localhost:3000/api/tv-fyers/auth/callback',
      LOG_LEVEL: 'silent',
    }));
    const fyersAuth = require('../../services/tv-fyers-auth');
    await expect(fyersAuth.generateSession('bad-code')).rejects.toThrow(/invalid auth code/);
  });

  test('throws on non-200 HTTP status', async () => {
    jest.doMock('https', () => ({
      request: mockHttpsRequest(500, { message: 'server error' }),
    }));
    jest.doMock('../../config', () => ({
      FYERS_CLIENT_ID: 'test-client-100',
      FYERS_SECRET_KEY: 'test-secret-key',
      FYERS_REDIRECT_URL: 'http://localhost:3000/api/tv-fyers/auth/callback',
      LOG_LEVEL: 'silent',
    }));
    const fyersAuth = require('../../services/tv-fyers-auth');
    await expect(fyersAuth.generateSession('some-code')).rejects.toThrow();
  });
});
