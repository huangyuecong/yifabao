export interface AuthUser {
    subject: string;
    display_name: string;
    department?: string;
    roles: string[];
    permissions: string[];
    username?: string;
    avatar?: string;
}

export interface AuthState {
    authenticated: boolean;
    user?: AuthUser;
}

export interface PasswordPublicKey {
    algorithm: "SM2";
    key: string;
}
