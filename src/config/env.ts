/**
 * Environment configuration
 * Access environment variables with type safety
 */

interface AppEnv {
  ENVIRONMENT: 'development' | 'staging' | 'production'
  IS_DEV: boolean
  IS_PROD: boolean
}

function getEnv(): AppEnv {
  const env = (import.meta.env.VITE_ENVIRONMENT ||
    import.meta.env.MODE ||
    'development') as AppEnv['ENVIRONMENT']

  return {
    ENVIRONMENT: env,
    IS_DEV: env === 'development',
    IS_PROD: env === 'production',
  }
}

export const env = getEnv()
