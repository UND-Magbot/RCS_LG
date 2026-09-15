export type CruiseRouteSite = {
  id: string;
  name: string;
  floor: string;
};

export type CruiseRoute = {
  id: string;
  routeName: string;
  businessId: string;
  businessName: string;
  sites: CruiseRouteSite[];
  siteCount: number;
  createTime: string;
};

export type CruiseRouteFormState = {
  routeName: string;
  businessId: string;
  sites: CruiseRouteSite[];
};

export type CruiseRouteTableProps = {
  routes: CruiseRoute[];
  onEdit: (routeId: string) => void;
  onDelete: (routeId: string) => void;
};

export type CruiseRouteModalProps = {
  open: boolean;
  onClose: () => void;
  onSave: (route: CruiseRoute) => void;
  editRoute: CruiseRoute | null;
};
