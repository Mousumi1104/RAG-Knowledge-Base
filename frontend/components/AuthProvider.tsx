"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  getCachedUser,
  getMe,
  getToken,
  logout as apiLogout,
  type CurrentUser,
} from "@/services/api";

interface AuthContextValue {
  /** The current authenticated user, or null when signed out. */
  user: CurrentUser | null;
  /** True until the initial session check (token → /auth/me) completes. */
  loading: boolean;
  /** Convenience flags derived from `user`. */
  isAuthed: boolean;
  isAdmin: boolean;
  /** Re-hydrate the user from the stored token (call after login/register). */
  refresh: () => Promise<CurrentUser | null>;
  /** Clear the token + cached user and drop the in-memory session. */
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Provides the current-user session to the app.
 *
 * On mount it reads the stored token and, if present, fetches `/auth/me` to
 * hydrate the user (id/email/role). A 401 (expired/invalid token) clears the
 * session silently. Children read the session via {@link useAuth}.
 */
export default function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(getCachedUser());
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async (): Promise<CurrentUser | null> => {
    if (!getToken()) {
      setUser(null);
      return null;
    }
    try {
      const me = await getMe();
      setUser(me);
      return me;
    } catch {
      // AuthError (or any failure) → treat as signed out. getMe/authedFetch
      // already clears the token on a 401.
      setUser(null);
      return null;
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    void (async () => {
      await refresh();
      if (mounted) {
        setLoading(false);
      }
    })();
    return () => {
      mounted = false;
    };
  }, [refresh]);

  const signOut = useCallback(() => {
    apiLogout();
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      isAuthed: user !== null,
      isAdmin: user?.role === "admin",
      refresh,
      signOut,
    }),
    [user, loading, refresh, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Read the auth session. Must be used within an {@link AuthProvider}. */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
