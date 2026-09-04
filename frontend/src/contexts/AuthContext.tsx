import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, clearTokens } from '../api/client';
import type { TokenResponse, User } from '../types';

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  authenticated: boolean;
  updateProfile: (first_name: string, last_name: string) => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  register: (payload: { email: string; password: string; confirm_password: string; first_name: string; last_name: string }) => Promise<void>;
  verifyEmail: (email: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
  reloadUser: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const reloadUser = async () => {
    try {
      setUser(await api.me());
    } catch {
      clearTokens();
      setUser(null);
    }
  };

  useEffect(() => {
    if (!localStorage.getItem('wb_optimizer_access_token')) {
      setLoading(false);
      return;
    }
    void reloadUser().finally(() => setLoading(false));
  }, []);

  const value = useMemo<AuthContextValue>(() => ({
    user,
    loading,
    authenticated: Boolean(user),
    login: async (email, password) => {
      const result: TokenResponse = await api.login(email, password);
      setUser(result.user);
    },
    updateProfile: async (first_name, last_name) => {
      setUser(await api.updateProfile(first_name, last_name));
    },
    register: async (payload) => { await api.register(payload); },
    verifyEmail: async (email, code) => {
      const result = await api.verifyEmail(email, code);
      setUser(result.user);
    },
    logout: async () => { await api.logout(); setUser(null); },
    reloadUser,
  }), [loading, user]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}
