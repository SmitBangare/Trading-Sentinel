import React, { useState, useEffect } from 'react';
import { Clock } from 'lucide-react';
import { postClient } from '../api/client';

export default function Login({ healthData, onLoggedIn }) {
  const [istTime, setIstTime] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const timer = setInterval(() => {
      const timeString = new Date().toLocaleTimeString('en-US', {
        timeZone: 'Asia/Kolkata',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
      setIstTime(`${timeString} IST`);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const isMarketOpen = healthData?.market_open || false;
  const lastLogin = healthData?.last_login_time
    ? new Date(healthData.last_login_time).toLocaleString()
    : 'Never';

  const submit = async (event) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      await postClient('/api/auth/password-login', { password });
      if (onLoggedIn) await onLoggedIn();
    } catch (err) {
      setError(err.info?.message || 'Incorrect password');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 px-4">
      <div className="max-w-md w-full bg-gray-900 border border-gray-800 rounded-xl shadow-2xl p-8 text-center">
        <h1 className="text-3xl font-bold text-white mb-2">Quant Gateway</h1>
        <p className="text-gray-400 mb-8">System Access & Execution Node</p>

        <div className="bg-gray-800 rounded-lg p-4 mb-8 flex justify-between items-center text-sm border border-gray-700">
          <div className="flex items-center space-x-2 text-gray-300">
            <Clock size={16} className="text-blue-400" />
            <span className="font-mono">{istTime}</span>
          </div>
          <div className={`px-2 py-1 rounded font-medium ${isMarketOpen ? 'bg-green-900/50 text-green-400' : 'bg-yellow-900/50 text-yellow-500'}`}>
            Market {isMarketOpen ? 'OPEN' : 'CLOSED'}
          </div>
        </div>

        <form onSubmit={submit} className="text-left">
          <label className="block text-xs text-gray-400 mb-1">Dashboard password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            className="w-full rounded border border-gray-700 bg-gray-950 p-3 text-gray-100 mb-3"
          />
          {error && <p className="text-red-400 text-sm mb-3">{error}</p>}
          <button
            type="submit"
            disabled={submitting || !password}
            className="block w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white font-bold py-3 px-4 rounded transition-colors"
          >
            {submitting ? 'Checking…' : 'Unlock Dashboard'}
          </button>
        </form>

        <a
          href="/api/auth/login"
          className="mt-4 block text-xs text-gray-500 hover:text-gray-400 underline"
        >
          (Optional) Connect Zerodha broker account instead
        </a>

        <div className="mt-6 text-xs text-gray-500">
          Last login: {lastLogin}
        </div>
      </div>
    </div>
  );
}
