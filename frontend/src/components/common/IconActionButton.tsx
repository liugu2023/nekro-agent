import { IconButton, IconButtonProps, SxProps, Theme } from '@mui/material'
import { forwardRef } from 'react'
import { ICON_ACTION_BUTTON_VARIANTS } from '../../theme/variants'

export type IconActionTone = 'subtle' | 'primary' | 'danger'

// 允许透传 MUI color（运行时本就透传给 IconButton 并生效）；新代码优先使用 tone 体系
interface IconActionButtonProps extends IconButtonProps {
  tone?: IconActionTone
}

const IconActionButton = forwardRef<HTMLButtonElement, IconActionButtonProps>(function IconActionButton(
  { tone = 'subtle', sx, ...props },
  ref,
) {
  const mergedSx = (
    sx
      ? [ICON_ACTION_BUTTON_VARIANTS[tone].styles, sx]
      : ICON_ACTION_BUTTON_VARIANTS[tone].styles
  ) as SxProps<Theme>

  return (
    <IconButton
      {...props}
      ref={ref}
      sx={mergedSx}
    />
  )
})

export default IconActionButton
