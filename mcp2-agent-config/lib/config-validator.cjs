"use strict";

const ALLOWED_STATUS = new Set(["active", "pending", "disabled"]);
const ALLOWED_AUTH = new Set(["api_key", "oauth2", "pat", "service_account"]);

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateConfig(config, options = {}) {
  const strictMode = Boolean(options.strict);
  const errors = [];
  const warnings = [];

  if (!isObject(config)) {
    errors.push("Root must be an object.");
    return { ok: false, errors, warnings };
  }

  if (!isObject(config.meta)) {
    errors.push("`meta` must be an object.");
  } else {
    const metaRequired = ["profile_name", "owner", "environment", "last_updated"];
    for (const key of metaRequired) {
      if (typeof config.meta[key] !== "string" || !config.meta[key].trim()) {
        errors.push(`meta.${key} is required and must be a non-empty string.`);
      }
    }
    if (typeof config.meta.rotation_policy_days !== "number") {
      warnings.push("meta.rotation_policy_days should be a number.");
    }
  }

  if (!isObject(config.integrations)) {
    errors.push("`integrations` must be an object.");
  } else {
    const names = Object.keys(config.integrations);
    if (names.length === 0) {
      warnings.push("No integrations found.");
    }

    for (const name of names) {
      const item = config.integrations[name];
      if (!isObject(item)) {
        errors.push(`integrations.${name} must be an object.`);
        continue;
      }

      if (!ALLOWED_STATUS.has(item.status)) {
        errors.push(
          `integrations.${name}.status must be one of: ${[...ALLOWED_STATUS].join(", ")}.`
        );
      }

      if (!ALLOWED_AUTH.has(item.auth_type)) {
        errors.push(
          `integrations.${name}.auth_type must be one of: ${[...ALLOWED_AUTH].join(", ")}.`
        );
      }

      if (typeof item.base_url !== "string" || !/^https?:\/\//.test(item.base_url)) {
        errors.push(`integrations.${name}.base_url must be a valid http(s) URL string.`);
      }

      if (!isObject(item.credential_fields)) {
        errors.push(`integrations.${name}.credential_fields must be an object.`);
      } else {
        const entries = Object.entries(item.credential_fields);
        if (entries.length === 0) {
          warnings.push(`integrations.${name}.credential_fields is empty.`);
        }

        const missing = [];
        for (const [key, value] of entries) {
          if (typeof value !== "string") {
            errors.push(`integrations.${name}.credential_fields.${key} must be a string.`);
            continue;
          }
          if (!value.trim()) {
            missing.push(key);
          }
        }

        if (item.status === "active" && missing.length > 0) {
          errors.push(
            `integrations.${name} is active but has empty credential_fields: ${missing.join(", ")}.`
          );
        } else if (item.status !== "active" && missing.length > 0) {
          warnings.push(
            `integrations.${name} has empty credential_fields (${missing.join(", ")}).`
          );
        }
      }

      if (!Array.isArray(item.scopes)) {
        errors.push(`integrations.${name}.scopes must be an array.`);
      } else if (item.scopes.some((s) => typeof s !== "string" || !s.trim())) {
        errors.push(`integrations.${name}.scopes must contain non-empty strings only.`);
      } else if (item.scopes.length === 0) {
        warnings.push(`integrations.${name}.scopes is empty.`);
      }
    }
  }

  if (strictMode && warnings.length > 0) {
    errors.push(`Strict mode enabled: ${warnings.length} warning(s) treated as errors.`);
  }

  return { ok: errors.length === 0, errors, warnings };
}

module.exports = {
  validateConfig
};
