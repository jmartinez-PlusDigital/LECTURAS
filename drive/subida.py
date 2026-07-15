import io

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .cliente import obtener_servicio
from .exceptions import DriveUploadError


def subir_archivo(nombre_archivo: str, contenido: bytes, mime_type: str, carpeta_id: str) -> str:
    """Sube un archivo a una carpeta de Drive y devuelve su URL (webViewLink).

    Lanza `DriveUploadError` ante cualquier fallo (credenciales, red, permisos,
    carpeta inexistente) para que el llamador pueda aislar el error por
    contrato sin detener el resto del ciclo de facturación.
    """
    if not carpeta_id:
        raise DriveUploadError("El contrato no tiene carpeta_drive_destino_id configurada.")

    try:
        servicio = obtener_servicio()
        media = MediaIoBaseUpload(io.BytesIO(contenido), mimetype=mime_type, resumable=False)
        metadata = {"name": nombre_archivo, "parents": [carpeta_id]}
        archivo = (
            servicio.files()
            .create(body=metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )
    except HttpError as exc:
        raise DriveUploadError(f"Error de la API de Drive al subir '{nombre_archivo}': {exc}") from exc
    except OSError as exc:
        raise DriveUploadError(f"Error de conexión al subir '{nombre_archivo}' a Drive: {exc}") from exc

    return archivo.get("webViewLink") or f"https://drive.google.com/file/d/{archivo['id']}/view"


def subir_documentos_factura(
    *,
    carpeta_id: str,
    nombre_base: str,
    pdf_bytes: bytes,
    excel_bytes: bytes,
) -> tuple[str, str]:
    """Sube el PDF y el Excel de una factura a la carpeta del contrato.

    Devuelve (pdf_url, excel_url). Si falla cualquiera de las dos subidas,
    lanza DriveUploadError sin haber persistido nada en la base de datos
    (la persistencia solo ocurre si ambas subidas terminan con éxito).
    """
    pdf_url = subir_archivo(f"{nombre_base}.pdf", pdf_bytes, "application/pdf", carpeta_id)
    excel_url = subir_archivo(
        f"{nombre_base}.xlsx",
        excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        carpeta_id,
    )
    return pdf_url, excel_url
