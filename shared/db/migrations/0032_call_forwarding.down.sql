DROP TABLE IF EXISTS forwarded_calls;
ALTER TABLE phone_numbers DROP COLUMN IF EXISTS forward_line_name;
ALTER TABLE phone_numbers DROP COLUMN IF EXISTS forward_fallback;
ALTER TABLE phone_numbers DROP COLUMN IF EXISTS forward_confirm;
ALTER TABLE phone_numbers DROP COLUMN IF EXISTS forward_ring_s;
ALTER TABLE phone_numbers DROP COLUMN IF EXISTS forward_contact_id;
ALTER TABLE phone_numbers DROP COLUMN IF EXISTS forward_mode;
DROP TABLE IF EXISTS forward_contacts;
