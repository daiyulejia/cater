"""Conservative SQLite object-field lookup, without decoding unrelated bodies."""

# SQLite paths and Python dict lookup differ for NUL and duplicate keys. Callers
# either parse such a row completely or report metadata unavailable.
UNAMBIGUOUS_OBJECT_SQL = (
    "CASE WHEN json_valid(payload_json) THEN CASE WHEN json_type(payload_json) = 'object' THEN "
    "(SELECT count(*) = count(DISTINCT CAST(key AS BLOB)) AND "
    "count(*) FILTER (WHERE instr(CAST(key AS BLOB), x'00') > 0) = 0 "
    "FROM json_each(payload_json)) ELSE 0 END ELSE 0 END"
)
