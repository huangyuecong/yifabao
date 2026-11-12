// Standalone workspaces use the backend's local owner and never require a login.
export const isStandaloneMode = import.meta.env.VITE_CONTENT_AUTH_MODE === 'standalone'
