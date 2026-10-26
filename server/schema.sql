-- Shared database for claude-browser-atlas (Supabase / Postgres), run like a repository:
-- anyone submits to `submissions` (the review queue), a maintainer merges, everyone reads what was merged.
-- The public tables (experience, map_fixes, traps) are written only through merge_submission(),
-- or directly by a maintainer. The queue is not readable by anyone but maintainers.
-- Rows are data that other people's agents will read, so the database itself limits their shape:
-- closed vocabularies where possible, short strings, bounded JSON. Clients sanitize again on read.
--
-- Setup: create a Supabase project, run this file in the SQL editor, create the maintainer user
-- (Authentication > Users), then:  insert into maintainers (user_id) values ('<that user id>');

create table if not exists maintainers (
  user_id uuid primary key references auth.users (id) on delete cascade
);

create table if not exists experience (
  id             text primary key check (id ~ '^[0-9a-f]{32}$'),
  site           text not null check (site ~ '^[a-z0-9_]{1,64}$'),
  keywords       jsonb not null check (jsonb_typeof(keywords) = 'array' and octet_length(keywords::text) <= 2000),
  route          jsonb not null check (jsonb_typeof(route) = 'array' and octet_length(route::text) <= 4000),
  outcome        text not null check (outcome in ('success', 'partial', 'fail', 'blocked')),
  quality        real check (quality between 0 and 1),
  quality_source text check (quality_source in ('self', 'second_source', 'human')),
  seconds        real check (seconds >= 0 and seconds < 86400),
  day            date,
  created_at     timestamptz not null default now(),
  author         uuid not null default auth.uid()
);

create table if not exists map_fixes (
  id         text primary key check (id ~ '^[0-9a-f]{32}$'),
  kind       text not null check (kind in ('rename', 'new')),
  site       text not null check (site ~ '^[a-z0-9_]{1,64}$'),
  page       text check (char_length(page) <= 64),
  role       text not null check (role in ('button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox',
                                           'radio', 'tab', 'menuitem', 'option', 'switch', 'slider',
                                           'spinbutton', 'listbox')),
  name       text check (char_length(name) <= 80),
  "from"     text check (char_length("from") <= 80),
  "to"       text check (char_length("to") <= 80),
  url        text check (char_length(url) <= 300 and url !~ '[?#]'),
  seen       integer check (seen between 0 and 100000),
  created_at timestamptz not null default now(),
  author     uuid not null default auth.uid()
);

alter table maintainers enable row level security;
alter table experience  enable row level security;
alter table map_fixes   enable row level security;

-- read: everyone (that is the point of sharing)
create policy experience_read on experience for select using (true);
create policy map_fixes_read  on map_fixes  for select using (true);

-- write: maintainers only, and only as themselves; nobody updates or deletes through the API
create policy experience_write on experience for insert
  with check (author = auth.uid() and exists (select 1 from maintainers m where m.user_id = auth.uid()));
create policy map_fixes_write on map_fixes for insert
  with check (author = auth.uid() and exists (select 1 from maintainers m where m.user_id = auth.uid()));

-- the maintainers table: a signed-in user can see only their own row (so the policies above can
-- ask "am I a maintainer?"); nobody can list maintainers or write the table through the API
create policy maintainers_self on maintainers for select to authenticated using (user_id = auth.uid());

create index if not exists experience_site on experience (site);
create index if not exists map_fixes_site  on map_fixes (site);

-- ── lessons: one sentence about a pitfall on a mapped page ─────────────────
create table if not exists traps (
  id         text primary key check (id ~ '^[0-9a-f]{32}$'),
  site       text not null check (site ~ '^[a-z0-9_]{1,64}$'),
  page       text check (char_length(page) <= 64),
  text       text not null check (char_length(text) between 5 and 200 and text !~ '[<>{}`]'),
  day        date,
  created_at timestamptz not null default now(),
  author     uuid not null default auth.uid()
);
alter table traps enable row level security;
create policy traps_read on traps for select using (true);
create policy traps_write on traps for insert
  with check (author = auth.uid() and exists (select 1 from maintainers m where m.user_id = auth.uid()));
create index if not exists traps_site on traps (site);

-- ── the review queue ───────────────────────────────────────────────────────
-- Anyone may insert, nobody but maintainers may read, nobody updates or deletes through the API.
create table if not exists submissions (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null check (kind in ('experience', 'map_fix', 'trap')),
  site        text not null check (site ~ '^[a-z0-9_]{1,64}$'),
  payload     jsonb not null check (jsonb_typeof(payload) = 'object' and octet_length(payload::text) <= 4000),
  status      text not null default 'pending' check (status in ('pending', 'merged', 'rejected')),
  created_at  timestamptz not null default now(),
  reviewed_at timestamptz
);
alter table submissions enable row level security;
create policy submissions_submit on submissions for insert to anon, authenticated
  with check (status = 'pending' and reviewed_at is null);
create policy submissions_review on submissions for select
  using (exists (select 1 from maintainers m where m.user_id = auth.uid()));
create index if not exists submissions_pending on submissions (created_at) where status = 'pending';

-- a flood cannot fill the database: 120 submissions a minute and 5000 waiting, for everyone together
create or replace function submissions_guard() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from submissions where created_at > now() - interval '1 minute') >= 120 then
    raise exception 'too many submissions right now, try again in a minute';
  end if;
  if (select count(*) from submissions where status = 'pending') >= 5000 then
    raise exception 'the review queue is full';
  end if;
  new.status := 'pending';
  new.reviewed_at := null;
  new.created_at := now();
  return new;
end $$;
drop trigger if exists submissions_guard on submissions;
create trigger submissions_guard before insert on submissions for each row execute function submissions_guard();

-- merge: maintainers only; the target table's own checks apply to the payload again
create or replace function merge_submission(sid uuid) returns text
language plpgsql security definer set search_path = public as $$
declare
  s submissions;
  p jsonb;
begin
  if not exists (select 1 from maintainers m where m.user_id = auth.uid()) then
    raise exception 'maintainers only';
  end if;
  select * into s from submissions where id = sid and status = 'pending' for update;
  if not found then
    raise exception 'no pending submission %', sid;
  end if;
  p := s.payload;
  if s.kind = 'experience' then
    insert into experience (id, site, keywords, route, outcome, quality, quality_source, seconds, day, author)
    values (p->>'id', s.site, p->'keywords', p->'route', p->>'outcome', (p->>'quality')::real,
            p->>'quality_source', (p->>'seconds')::real, (p->>'day')::date, auth.uid())
    on conflict (id) do nothing;
  elsif s.kind = 'map_fix' then
    insert into map_fixes (id, kind, site, page, role, name, "from", "to", url, seen, author)
    values (p->>'id', p->>'kind', s.site, p->>'page', p->>'role', p->>'name', p->>'from', p->>'to',
            p->>'url', (p->>'seen')::integer, auth.uid())
    on conflict (id) do nothing;
  else
    insert into traps (id, site, page, text, day, author)
    values (p->>'id', s.site, nullif(p->>'page', ''), p->>'text', (p->>'day')::date, auth.uid())
    on conflict (id) do nothing;
  end if;
  update submissions set status = 'merged', reviewed_at = now() where id = sid;
  return 'merged';
end $$;

create or replace function reject_submission(sid uuid) returns text
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from maintainers m where m.user_id = auth.uid()) then
    raise exception 'maintainers only';
  end if;
  update submissions set status = 'rejected', reviewed_at = now() where id = sid and status = 'pending';
  if not found then
    raise exception 'no pending submission %', sid;
  end if;
  return 'rejected';
end $$;

revoke execute on function merge_submission(uuid), reject_submission(uuid) from public, anon;
grant execute on function merge_submission(uuid), reject_submission(uuid) to authenticated;

-- ── optional: mail the maintainer when submissions arrive ──────────────────
-- Needs three Vault secrets (Project Settings > Vault): resend_api_key, notify_to, notify_from
-- (a sender on a domain verified in Resend). Without them nothing is sent. At most one mail an hour
-- (a shared Resend free quota is 100 a day), and the mail carries only the pending count: submitted text never leaves the database.
create extension if not exists pg_net with schema extensions;
create table if not exists notify_state (id int primary key default 1 check (id = 1), last_sent timestamptz);
alter table notify_state enable row level security;          -- no policies: invisible to the API

create or replace function notify_submissions() returns trigger
language plpgsql security definer set search_path = public, extensions as $$
declare
  k text; to_addr text; from_addr text; pending int; last timestamptz;
begin
  select decrypted_secret into k from vault.decrypted_secrets where name = 'resend_api_key';
  select decrypted_secret into to_addr from vault.decrypted_secrets where name = 'notify_to';
  select decrypted_secret into from_addr from vault.decrypted_secrets where name = 'notify_from';
  if k is null or to_addr is null or from_addr is null then
    return null;
  end if;
  select last_sent into last from notify_state where id = 1;
  if last is not null and last > now() - interval '1 hour' then
    return null;
  end if;
  insert into notify_state (id, last_sent) values (1, now())
    on conflict (id) do update set last_sent = excluded.last_sent;
  select count(*) into pending from submissions where status = 'pending';
  perform net.http_post(
    url := 'https://api.resend.com/emails',
    headers := jsonb_build_object('Authorization', 'Bearer ' || k, 'Content-Type', 'application/json'),
    body := jsonb_build_object(
      'from', from_addr, 'to', jsonb_build_array(to_addr),
      'subject', 'browser-atlas: ' || pending || ' submission(s) waiting for review',
      'text', pending || ' submission(s) are waiting in the browser-atlas review queue.' || chr(10) ||
              'Review them on your machine: python -m browser_atlas share review' || chr(10) ||
              'This mail never contains submitted text.'));
  return null;
exception when others then
  return null;                                                -- a mail problem must never block a submission
end $$;
create trigger submissions_notify after insert on submissions
  for each statement execute function notify_submissions();
