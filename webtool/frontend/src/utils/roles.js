export const roleLevels = { viewer: 1, editor: 2, admin: 3 };

export function can(user, role) {
  return roleLevels[user?.role] >= roleLevels[role];
}
