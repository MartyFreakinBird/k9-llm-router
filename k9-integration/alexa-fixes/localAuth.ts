/**
 * localAuth.ts — K-9 Local Development Auth
 *
 * Replaces replitAuth.ts for WSL2 / standalone deployment.
 * In LOCAL_AUTH mode: session is established automatically as a service account.
 * In PRODUCTION mode: set AUTH_MODE=jwt and provide JWT_SECRET.
 *
 * Drop-in compatible — exports same interface as replitAuth.ts:
 *   setupAuth(app), isAuthenticated, getSession
 */

import session from "express-session";
import passport from "passport";
import { Strategy as LocalStrategy } from "passport-local";
import type { Express, RequestHandler } from "express";
import connectPg from "connect-pg-simple";
import { storage } from "./storage";

const AUTH_MODE = process.env.AUTH_MODE || "local";      // "local" | "jwt"
const SESSION_SECRET = process.env.SESSION_SECRET || "k9-local-dev-secret-change-in-prod";

// Service account — shape includes claims.sub for compatibility with routes.ts
// (routes.ts was written against Replit OIDC which uses req.user.claims.sub)
const SERVICE_USER = {
  id: "k9-service-account",
  email: process.env.SERVICE_ACCOUNT_EMAIL || "timaugustus@outlook.com",
  firstName: "VectOS",
  lastName: "Carbon",
  claims: {
    sub: "k9-service-account",   // ← routes.ts reads req.user.claims.sub
    email: process.env.SERVICE_ACCOUNT_EMAIL || "timaugustus@outlook.com",
    first_name: "VectOS",
    last_name: "Carbon",
  },
};

export function getSession() {
  const sessionTtl = 7 * 24 * 60 * 60 * 1000;

  // Use pg session store if DATABASE_URL is set, else memory store
  if (process.env.DATABASE_URL) {
    const pgStore = connectPg(session);
    return session({
      secret: SESSION_SECRET,
      store: new pgStore({
        conString: process.env.DATABASE_URL,
        createTableIfMissing: true,
        ttl: sessionTtl,
        tableName: "sessions",
      }),
      resave: false,
      saveUninitialized: false,
      cookie: { httpOnly: true, secure: false, maxAge: sessionTtl },
    });
  }

  console.warn("[Auth] No DATABASE_URL — using in-memory session store (non-persistent)");
  return session({
    secret: SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: { httpOnly: true, secure: false, maxAge: sessionTtl },
  });
}

export async function setupAuth(app: Express) {
  app.set("trust proxy", 1);
  app.use(getSession());
  app.use(passport.initialize());
  app.use(passport.session());

  if (AUTH_MODE === "local") {
    console.log("[Auth] LOCAL mode — auto-authenticating as service account");

    // Auto-login strategy: any username/password accepted in local mode
    passport.use(
      new LocalStrategy(async (username, _password, done) => {
        try {
          await storage.upsertUser(SERVICE_USER);
          done(null, SERVICE_USER);
        } catch (err) {
          done(err);
        }
      })
    );

    // Auto-inject service account on every request (no login wall)
    app.use((req, _res, next) => {
      if (!req.isAuthenticated()) {
        req.login(SERVICE_USER, { session: true }, (err) => {
          if (err) console.warn("[Auth] Auto-login failed:", err.message);
          return next();
        });
      } else {
        next();
      }
    });
  }

  passport.serializeUser((user: any, cb) => cb(null, user));
  passport.deserializeUser((user: any, cb) => cb(null, user));

  // Stub login/logout routes — in local mode just redirect home
  app.get("/api/login", (_req, res) => res.redirect("/"));
  app.get("/api/callback", (_req, res) => res.redirect("/"));
  app.get("/api/logout", (req, res) => {
    req.logout(() => res.redirect("/"));
  });
}

/**
 * isAuthenticated middleware — drop-in replacement.
 * In LOCAL mode: always passes through and ensures user object is set.
 * In JWT mode: validates Authorization: Bearer <token>.
 */
export const isAuthenticated: RequestHandler = (req, res, next) => {
  if (AUTH_MODE === "local") {
    // Ensure user object is set for downstream handlers
    if (!req.user) {
      (req as any).user = SERVICE_USER;
    }
    return next();
  }

  // JWT mode — validate Authorization: Bearer <token>
  const authHeader = req.headers.authorization;
  if (!authHeader?.startsWith("Bearer ")) {
    return res.status(401).json({ message: "Unauthorized" });
  }

  try {
    const jwt = require("jsonwebtoken");
    const token = authHeader.slice(7);
    const decoded = jwt.verify(token, process.env.JWT_SECRET!);
    (req as any).user = decoded;
    return next();
  } catch {
    return res.status(401).json({ message: "Unauthorized" });
  }
};
