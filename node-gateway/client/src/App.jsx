import React, { useState } from 'react';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Positions from './pages/Positions';
import BacktestLab from './pages/BacktestLab';
import ResearchCenter from './pages/ResearchCenter';
import TvFyers from './pages/TvFyers';
import { useHealth } from './hooks/useHealth';
import { useAuthStatus } from './hooks/useAuthStatus';

export default function App() {
  const { health, isLoading, isError } = useHealth();
  const { authenticated, isLoading: authLoading, mutate: refreshAuth } = useAuthStatus();
  const [currentView, setCurrentView] = useState('DASHBOARD');

  // Show a dark loading screen while strictly checking session auth
  if ((isLoading && !health) || authLoading) {
    return <div className="min-h-screen bg-gray-950 flex items-center justify-center text-gray-500 font-mono">Initializing System...</div>;
  }

  // If endpoint fails entirely, show safe fallback
  if (isError) {
    return <div className="min-h-screen bg-gray-950 flex items-center justify-center text-red-500 font-mono">Data unavailable - retrying</div>;
  }

  // Dashboard access is gated on the browser session, not on whether a
  // Zerodha broker token happens to be armed -- see useAuthStatus.js.
  if (!authenticated) {
    return <Login healthData={health} onLoggedIn={refreshAuth} />;
  }

  // Basic View Router
  if (currentView === 'BACKTESTS') {
    return <BacktestLab navigateToDashboard={() => setCurrentView('DASHBOARD')} />;
  }
  if (currentView === 'RESEARCH') {
    return <ResearchCenter navigateToDashboard={() => setCurrentView('DASHBOARD')} navigateToBacktests={() => setCurrentView('BACKTESTS')} />;
  }
  if (currentView === 'POSITIONS') {
    return <Positions navigateToDashboard={() => setCurrentView('DASHBOARD')} />;
  }
  if (currentView === 'TV_FYERS') {
    return <TvFyers navigateToDashboard={() => setCurrentView('DASHBOARD')} />;
  }
  return (
    <Dashboard
      healthData={health}
      navigateToPositions={() => setCurrentView('POSITIONS')}
      navigateToBacktests={() => setCurrentView('BACKTESTS')}
      navigateToResearch={() => setCurrentView('RESEARCH')}
      navigateToTvFyers={() => setCurrentView('TV_FYERS')}
    />
  );
}
