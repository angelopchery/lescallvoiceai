ALTER TABLE wa_sends     DROP COLUMN IF EXISTS media_id;
ALTER TABLE wa_sends     DROP COLUMN IF EXISTS project;
ALTER TABLE wa_playbooks DROP COLUMN IF EXISTS media_map;
ALTER TABLE wa_media     DROP COLUMN IF EXISTS public_token;
ALTER TABLE wa_templates DROP COLUMN IF EXISTS source;
ALTER TABLE wa_accounts  DROP COLUMN IF EXISTS webhook_secret_enc;
ALTER TABLE wa_accounts  DROP COLUMN IF EXISTS webhook_token;
ALTER TABLE wa_accounts  DROP COLUMN IF EXISTS provider;
