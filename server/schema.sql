-- Shared database for claude-browser-atlas (Supabase / Postgres).
-- Everyone reads with the public anon key. Only accounts listed in `maintainers` write.
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

-- the maintainers table is not readable or writable through the API at all (no policies)

create index if not exists experience_site on experience (site);
create index if not exists map_fixes_site  on map_fixes (site);
