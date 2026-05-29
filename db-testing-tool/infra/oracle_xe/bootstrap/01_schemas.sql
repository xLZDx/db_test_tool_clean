-- Container-entrypoint script: runs ONCE on first container boot, as SYSTEM.
-- Creates one Oracle schema per OWNER referenced by any DRD source table.
-- The schema list is GENERIC -- the next-stage Python bootstrap (build_xe_schema.py)
-- pulls actual owner names from the saved PDM JSON.  Here we only create the
-- DBTOOL application user (already done by APP_USER env) and grant rights.

ALTER SESSION SET CONTAINER = XEPDB1;

-- The APP_USER env in docker-compose creates DBTOOL automatically.  Just
-- grant the privileges the runtime needs.
GRANT CREATE SESSION,
      CREATE ANY TABLE, INSERT ANY TABLE, SELECT ANY TABLE,
      CREATE ANY VIEW,  DROP ANY TABLE,   ALTER ANY TABLE,
      CREATE ANY INDEX, CREATE ANY SYNONYM
   TO DBTOOL;

ALTER USER DBTOOL QUOTA UNLIMITED ON USERS;
