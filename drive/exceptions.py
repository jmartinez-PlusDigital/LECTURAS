class DriveUploadError(Exception):
    """La subida a Google Drive falló (credenciales, red, permisos de carpeta, etc.).

    Debe capturarse por contrato: un fallo de subida no debe detener el
    procesamiento del resto de contratos en el ciclo de facturación.
    """
