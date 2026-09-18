; ml64 translation unit: a leaf function with C linkage (x64 has no underscore prefix).
.code
asm_triple PROC
    lea rax, [rcx+rcx*2]
    ret
asm_triple ENDP
END
