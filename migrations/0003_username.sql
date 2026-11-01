-- Existing UUIDs, passwords and ownership remain unchanged.
ALTER TABLE users ADD COLUMN username text;

WITH candidates AS (
    SELECT id, lower(split_part(email, '@', 1)) AS candidate FROM users
), counts AS (
    SELECT id, candidate,
           count(*) OVER (PARTITION BY candidate) AS matches
    FROM candidates
)
UPDATE users AS u
SET username = CASE
    WHEN c.candidate ~ '^[a-z0-9_]{3,32}$' AND c.matches = 1
      THEN c.candidate
    ELSE 'legacy_' || substr(replace(u.id::text, '-', ''), 1, 25)
END
FROM counts AS c
WHERE c.id = u.id;

ALTER TABLE users ALTER COLUMN username SET NOT NULL;
ALTER TABLE users ADD CONSTRAINT users_username_format
    CHECK (username ~ '^[a-z0-9_]{3,32}$');
CREATE UNIQUE INDEX users_username_unique ON users (username);
ALTER TABLE users DROP COLUMN email;
