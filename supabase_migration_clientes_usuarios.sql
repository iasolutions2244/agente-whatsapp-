-- ══════════════════════════════════════════════════════════════
-- MIGRACIÓN: tabla clientes_usuarios
-- Ejecutar en Supabase SQL Editor (una sola vez)
-- clientes.whatsapp_number_user NO se elimina (permite rollback)
-- ══════════════════════════════════════════════════════════════

-- 1. Crear tabla
CREATE TABLE IF NOT EXISTS clientes_usuarios (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id      UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    whatsapp_number TEXT NOT NULL,
    nombre          TEXT,
    rol             TEXT NOT NULL DEFAULT 'operador'
                        CHECK (rol IN ('dueño', 'gerente', 'operador')),
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT clientes_usuarios_whatsapp_unique UNIQUE (whatsapp_number)
);

-- 2. Índices
CREATE INDEX IF NOT EXISTS idx_clientes_usuarios_whatsapp
    ON clientes_usuarios (whatsapp_number);

CREATE INDEX IF NOT EXISTS idx_clientes_usuarios_cliente_id
    ON clientes_usuarios (cliente_id);

-- 3. Migrar datos existentes:
--    Por cada fila en clientes con whatsapp_number_user no nulo
--    que no tenga ya un registro en clientes_usuarios, crear uno con rol 'dueño'.
INSERT INTO clientes_usuarios (cliente_id, whatsapp_number, nombre, rol, activo)
SELECT
    c.id,
    c.whatsapp_number_user,
    c.nombre_restaurante,   -- nombre provisional; se puede editar luego
    'dueño',
    TRUE
FROM clientes c
WHERE c.whatsapp_number_user IS NOT NULL
  AND c.whatsapp_number_user <> ''
  AND NOT EXISTS (
      SELECT 1 FROM clientes_usuarios cu
      WHERE cu.whatsapp_number = c.whatsapp_number_user
  );

-- 4. Verificación: debe devolver tantas filas como restaurantes con número registrado
SELECT
    c.nombre_restaurante,
    cu.whatsapp_number,
    cu.rol,
    cu.activo
FROM clientes_usuarios cu
JOIN clientes c ON c.id = cu.cliente_id
ORDER BY c.nombre_restaurante;
