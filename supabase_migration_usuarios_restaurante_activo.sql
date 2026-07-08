-- ══════════════════════════════════════════════════════════════
-- MIGRACIÓN: usuarios.restaurante_activo_id / restaurante_activo_ts
-- Ejecutar en Supabase SQL Editor (una sola vez)
-- No destructiva: columnas nullable, no se tocan filas existentes.
--
-- Motivo: cuando un usuario tiene acceso a más de un restaurante y
-- escribe un mensaje que no nombra ninguno, el bot necesita recordar
-- cuál restaurante eligió la última vez (dentro de una ventana de 2
-- horas) para no preguntar en cada mensaje. Estas columnas guardan esa
-- elección por usuario.
-- ══════════════════════════════════════════════════════════════

-- 1. Agregar columnas (nullable: usuarios sin elección previa quedan en NULL)
ALTER TABLE usuarios
    ADD COLUMN IF NOT EXISTS restaurante_activo_id UUID REFERENCES clientes(id);

ALTER TABLE usuarios
    ADD COLUMN IF NOT EXISTS restaurante_activo_ts TIMESTAMPTZ;

-- 2. Verificación: debe mostrar las columnas nuevas, todas las filas
--    existentes con restaurante_activo_id / restaurante_activo_ts = NULL
SELECT id, whatsapp_number, nombre, restaurante_activo_id, restaurante_activo_ts
FROM usuarios
ORDER BY created_at DESC
LIMIT 10;
