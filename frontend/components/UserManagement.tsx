"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AuthError,
  deleteUser,
  disableUser,
  enableUser,
  listAllUsers,
  type UserOut,
} from "@/services/api";
import { useAuth } from "@/components/AuthProvider";

interface UserManagementProps {
  /** Called when an authed request returns 401 so the page can show login. */
  onAuthError: () => void;
}

/**
 * Admin user management.
 *
 * Lists all users (email, role, active) and lets an admin enable/disable a user
 * or delete one (with confirm). The backend rejects an admin disabling/deleting
 * themselves with a 400; that error message is surfaced inline (we don't try to
 * pre-guard the admin's own row beyond a visual hint).
 */
export default function UserManagement({ onAuthError }: UserManagementProps) {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState<UserOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof AuthError) {
        onAuthError();
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    },
    [onAuthError],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // The server already excludes super-admins (role === "admin"). Filter
      // defensively here too so no super-admin or the current admin's own
      // account is ever shown as a manageable/deletable row.
      const all = await listAllUsers();
      setUsers(all.filter((u) => u.role !== "admin" && u.id !== currentUser?.id));
    } catch (e) {
      handleError(e);
    } finally {
      setLoading(false);
    }
  }, [handleError, currentUser?.id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onToggleActive = useCallback(
    async (u: UserOut) => {
      setBusyId(u.id);
      setError(null);
      setNotice(null);
      try {
        // When `is_active` is unknown (backend may omit it), default to enabling.
        const updated = u.is_active === false ? await enableUser(u.id) : await disableUser(u.id);
        setNotice(
          `${updated.email} is now ${updated.is_active === false ? "disabled" : "enabled"}.`,
        );
        await refresh();
      } catch (e) {
        handleError(e);
      } finally {
        setBusyId(null);
      }
    },
    [refresh, handleError],
  );

  const onDelete = useCallback(
    async (u: UserOut) => {
      if (!window.confirm(`Delete user ${u.email}? This cannot be undone.`)) {
        return;
      }
      setBusyId(u.id);
      setError(null);
      setNotice(null);
      try {
        await deleteUser(u.id);
        setNotice(`Deleted ${u.email}.`);
        await refresh();
      } catch (e) {
        handleError(e);
      } finally {
        setBusyId(null);
      }
    },
    [refresh, handleError],
  );

  const renderActive = (u: UserOut): string => {
    if (u.is_active === undefined) return "—";
    return u.is_active ? "Active" : "Disabled";
  };

  return (
    <section className="card admin-dashboard">
      <div className="chat-header">
        <h2>User management</h2>
        <div className="chat-toolbar">
          <button className="secondary" onClick={() => void refresh()} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}

      {users.length === 0 ? (
        <p className="muted">{loading ? "Loading users..." : "No users."}</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>{u.id}</td>
                <td>{u.email}</td>
                <td>{u.role}</td>
                <td>{renderActive(u)}</td>
                <td className="actions">
                  <button
                    className="secondary"
                    onClick={() => void onToggleActive(u)}
                    disabled={busyId === u.id}
                  >
                    {u.is_active === false ? "Enable" : "Disable"}
                  </button>
                  <button
                    className="danger"
                    onClick={() => void onDelete(u)}
                    disabled={busyId === u.id}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
