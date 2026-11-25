-- 0031 — WhatsApp through a Business Solution Provider (Interakt), and one
-- brochure per project.
--
-- Why
-- ---
-- 0030 assumed every tenant brings a Meta Cloud API account (WABA id, phone
-- number id, system-user token). Pramukh Group's WhatsApp lives on Interakt
-- (interakt.ai), a Meta BSP: one API key, templates created and approved in
-- Interakt's dashboard, media passed as a public URL instead of an uploaded
-- media id, and delivery receipts through Interakt's own webhook. The row
-- shape is the same tenant account; what differs is WHO we talk to.
--
-- provider            'meta' (0030 behaviour, unchanged) | 'interakt'
-- webhook_token       the per-tenant path segment of the Interakt webhook URL
--                     (/api/whatsapp/webhook/interakt/<token>): Interakt's
--                     payload names no account, so the URL must.
-- webhook_secret_enc  the HMAC secret the tenant typed into Interakt, Fernet-
--                     encrypted like the API key (it authenticates receipts).
--
-- Templates registered by hand (Interakt has no "list templates" API) are
-- marked source='manual' so a Meta sync never deletes them.
--
-- Media gets a public_token: a BSP fetches the attachment from a URL, so each
-- file is reachable at /r/f/<token> — an unguessable address that serves one
-- file, like /r/<token> redirects to one URL.
--
-- A playbook can carry one attachment PER PROJECT (media_map: project name →
-- media id). Pramukh runs one agent per project; "the brochure" means the
-- brochure of the project the caller agreed to receive. The send records the
-- project it resolved and the media it picked, so an approver sees which file
-- goes out and a brochure uploaded later is picked up by a waiting send.

ALTER TABLE wa_accounts  ADD COLUMN IF NOT EXISTS provider           text NOT NULL DEFAULT 'meta';
ALTER TABLE wa_accounts  ADD COLUMN IF NOT EXISTS webhook_token      text UNIQUE;
ALTER TABLE wa_accounts  ADD COLUMN IF NOT EXISTS webhook_secret_enc text;

ALTER TABLE wa_templates ADD COLUMN IF NOT EXISTS source             text NOT NULL DEFAULT 'meta';

ALTER TABLE wa_media     ADD COLUMN IF NOT EXISTS public_token       text UNIQUE;

ALTER TABLE wa_playbooks ADD COLUMN IF NOT EXISTS media_map          text NOT NULL DEFAULT '{}';

ALTER TABLE wa_sends     ADD COLUMN IF NOT EXISTS project            text;
ALTER TABLE wa_sends     ADD COLUMN IF NOT EXISTS media_id           bigint;
