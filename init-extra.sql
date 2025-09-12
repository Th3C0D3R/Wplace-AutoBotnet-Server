-- Extra init script to ensure legacy database name exists
-- Creates an empty 'wplace' database if missing and grants privileges
DO
$$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_database WHERE datname = 'wplace') THEN
      PERFORM dblink_exec('dbname=' || current_database(), 'CREATE DATABASE wplace');
   END IF;
EXCEPTION WHEN undefined_function THEN
   -- dblink not installed, install and retry once
   EXECUTE 'CREATE EXTENSION IF NOT EXISTS dblink';
   IF NOT EXISTS (SELECT FROM pg_database WHERE datname = 'wplace') THEN
      PERFORM dblink_exec('dbname=' || current_database(), 'CREATE DATABASE wplace');
   END IF;
END;
$$;
